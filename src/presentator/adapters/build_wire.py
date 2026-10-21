"""The build wire's framing: requests and entries read, STATUS, OUTCOME, END sent."""

import os
import socket
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ValidationError

from presentator.contracts.decks import StepKind, is_a_plain_folder_name

MAGIC = b"PBW"
VERSION = 1
LENGTH = struct.Struct(">H")
END_COUNTS = struct.Struct(">IQ")
SIZE = struct.Struct(">Q")
MINIMUM_HEADER_BYTES = 2
MAXIMUM_HEADER_BYTES = 4096
MAXIMUM_PATH_BYTES = 1024
MAXIMUM_NAME_BYTES = 255
MAXIMUM_DEPTH = 32
CHUNK_BYTES = 64 * 1024
TOOLCHAIN_SCRATCH_NAME = b"node_modules"


class MessageKind(IntEnum):
    """The message kinds this end reads or writes."""

    HELLO = 1
    STEP = 2
    STATUS = 3
    OUTCOME = 5


class RecordTag(IntEnum):
    """The tags of the upload records that follow a STATUS."""

    DIR = 1
    FILE = 2
    END = 3


class WireRefusedError(Exception):
    """The peer sent something other than a valid HELLO, STEP or END."""


class EmptyHeader(BaseModel, extra="forbid", strict=True):
    """The header of a HELLO or STATUS: exactly the empty object."""


def _plain_folder_name(slug: str) -> str:
    if not is_a_plain_folder_name(slug):
        raise ValueError(slug)
    return slug


class StepHeader(BaseModel, extra="forbid", strict=True):
    """The header of a STEP: which step of which deck's build."""

    step: StepKind
    slug: Annotated[str, AfterValidator(_plain_folder_name)]


class FailedOutcome(BaseModel, extra="forbid", strict=True):
    """The header of an OUTCOME that carries no text: the build failed."""

    outcome: Literal["failed"] = "failed"
    text: None = None


_HEADER_MODELS: dict[int, type[EmptyHeader | StepHeader]] = {
    MessageKind.HELLO: EmptyHeader,
    MessageKind.STEP: StepHeader,
}


def _preamble(kind: MessageKind) -> bytes:
    return MAGIC + bytes([VERSION, kind])


def _read_exactly(connection: socket.socket, count: int) -> bytes:
    received = bytearray()
    while len(received) < count:
        chunk = connection.recv(count - len(received))
        if not chunk:
            raise WireRefusedError
        received += chunk
    return bytes(received)


def read_request(connection: socket.socket) -> EmptyHeader | StepHeader:
    """Read one HELLO or STEP preamble and header; raise WireRefusedError otherwise."""
    preamble = _read_exactly(connection, len(MAGIC) + 2)
    kind = preamble[len(MAGIC) + 1]
    if (
        preamble[: len(MAGIC) + 1] != MAGIC + bytes([VERSION])
        or kind not in _HEADER_MODELS
    ):
        raise WireRefusedError
    (length,) = LENGTH.unpack(_read_exactly(connection, LENGTH.size))
    if not MINIMUM_HEADER_BYTES <= length <= MAXIMUM_HEADER_BYTES:
        raise WireRefusedError
    try:
        return _HEADER_MODELS[kind].model_validate_json(
            _read_exactly(connection, length)
        )
    except ValidationError as error:
        raise WireRefusedError from error


def _send_message(connection: socket.socket, kind: MessageKind, header: bytes) -> None:
    connection.sendall(_preamble(kind) + LENGTH.pack(len(header)) + header)


def send_status(connection: socket.socket) -> None:
    """Write the STATUS message: the preamble and the empty header."""
    _send_message(connection, MessageKind.STATUS, b"{}")


def send_failed(connection: socket.socket) -> None:
    """Write the OUTCOME message: the build failed, with no text."""
    _send_message(
        connection, MessageKind.OUTCOME, FailedOutcome().model_dump_json().encode()
    )


def send_end(connection: socket.socket) -> None:
    """Write the END record of a reply that carries no entries."""
    connection.sendall(bytes([RecordTag.END]) + END_COUNTS.pack(0, 0))


def encode_record(header: StepHeader) -> bytes:
    """The request record: the validated header's JSON."""
    return header.model_dump_json().encode()


def write_record(descriptor: int, header: StepHeader) -> None:
    """Write the whole record from offset 0, leaving the shared file offset at 0."""
    record = memoryview(encode_record(header))
    written = 0
    while written < len(record):
        written += os.pwrite(descriptor, record[written:], written)


def read_record(descriptor: int) -> StepHeader:
    """Read the record from offset 0; refuse anything but a valid step header."""
    record = os.pread(descriptor, MAXIMUM_HEADER_BYTES + 1, 0)
    if len(record) > MAXIMUM_HEADER_BYTES:
        raise WireRefusedError
    try:
        return StepHeader.model_validate_json(record)
    except ValidationError as error:
        raise WireRefusedError from error


def _path_length_allowed(length: int) -> bool:
    return 1 <= length <= MAXIMUM_PATH_BYTES


def path_components(raw: bytes) -> tuple[bytes, ...]:
    """Split a raw entry path on "/"; raise WireRefusedError unless every rule holds."""
    components = tuple(raw.split(b"/"))
    if (
        not _path_length_allowed(len(raw))
        or len(components) > MAXIMUM_DEPTH
        or any(
            component in {b"", b".", b".."}
            or b"\0" in component
            or len(component) > MAXIMUM_NAME_BYTES
            for component in components
        )
    ):
        raise WireRefusedError
    return components


@dataclass
class _OpenDirectory:
    """A directory the receiver entered: its descriptor and its last made name."""

    descriptor: int
    name: bytes
    last_name: bytes = b""


def _read_path(connection: socket.socket) -> tuple[bytes, ...]:
    (length,) = LENGTH.unpack(_read_exactly(connection, LENGTH.size))
    if not _path_length_allowed(length):
        raise WireRefusedError
    return path_components(_read_exactly(connection, length))


def _enter_parent(
    open_directories: list[_OpenDirectory], components: tuple[bytes, ...]
) -> _OpenDirectory:
    """Pop to the entry's parent and check the entry's name rises; refuse otherwise."""
    parent_path = components[:-1]
    open_path = tuple(directory.name for directory in open_directories[1:])
    if open_path[: len(parent_path)] != parent_path:
        raise WireRefusedError
    while len(open_directories) > len(parent_path) + 1:
        os.close(open_directories.pop().descriptor)
    parent = open_directories[-1]
    if components[-1] <= parent.last_name or components == (TOOLCHAIN_SCRATCH_NAME,):
        raise WireRefusedError
    parent.last_name = components[-1]
    return parent


def _make_directory(parent: int, name: bytes) -> int:
    os.mkdir(name, 0o755, dir_fd=parent)
    return os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )


def _receive_file(connection: socket.socket, parent: int, name: bytes) -> int:
    """Create the file below the parent, copy its payload in chunks, return its size."""
    (size,) = SIZE.unpack(_read_exactly(connection, SIZE.size))
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
        dir_fd=parent,
    )
    try:
        remaining = size
        while remaining:
            chunk = memoryview(_read_exactly(connection, min(remaining, CHUNK_BYTES)))
            remaining -= len(chunk)
            while chunk:
                chunk = chunk[os.write(descriptor, chunk) :]
    finally:
        os.close(descriptor)
    return size


def receive_entries(connection: socket.socket, deck_descriptor: int) -> None:
    """Receive DIR and FILE entries below the borrowed deck, then a matching END.

    Raises WireRefusedError on any grammar, order or END fault; OSError passes through.
    Closes every descriptor it opened and leaves deck_descriptor open.
    """
    open_directories = [_OpenDirectory(deck_descriptor, b"")]
    count = total = 0
    try:
        while (tag := _read_exactly(connection, 1)[0]) != RecordTag.END:
            if tag not in {RecordTag.DIR, RecordTag.FILE}:
                raise WireRefusedError
            components = _read_path(connection)
            parent = _enter_parent(open_directories, components)
            if tag == RecordTag.DIR:
                open_directories.append(
                    _OpenDirectory(
                        _make_directory(parent.descriptor, components[-1]),
                        components[-1],
                    )
                )
            else:
                total += _receive_file(connection, parent.descriptor, components[-1])
            count += 1
        if END_COUNTS.unpack(_read_exactly(connection, END_COUNTS.size)) != (
            count,
            total,
        ):
            raise WireRefusedError
    finally:
        for directory in open_directories[1:]:
            os.close(directory.descriptor)
