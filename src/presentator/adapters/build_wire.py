"""The build wire's framing: requests and entries read, STATUS, OUTCOME, END sent."""

import errno
import os
import socket
import stat
import struct
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, StringConstraints, ValidationError

from presentator.contracts.decks import (
    FAILURE_TEXT_LIMIT,
    PDF_FILE,
    TALK_DIRECTORY,
    TOOLCHAIN_SCRATCH_DIRECTORY,
    StepKind,
    is_a_plain_folder_name,
)

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
MAXIMUM_ENTRIES = 100_000
MEGABYTE = 1024 * 1024
TOOLCHAIN_SCRATCH_NAME = TOOLCHAIN_SCRATCH_DIRECTORY.encode()


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
    """The header of an OUTCOME: the build failed, with the step's text if any."""

    outcome: Literal["failed"] = "failed"
    text: Annotated[str, StringConstraints(max_length=FAILURE_TEXT_LIMIT)] | None = None


class RefusalLimit(StrEnum):
    """Why a finished step's output was refused."""

    SHAPE = "shape"
    ENTRIES = "entries"
    MEGABYTES = "megabytes"


class BuiltOutcome(BaseModel, extra="forbid", strict=True):
    """The header of an OUTCOME: the build produced its declared output."""

    outcome: Literal["built"] = "built"


class RefusedOutcome(BaseModel, extra="forbid", strict=True):
    """The header of an OUTCOME: the output broke a contract or a limit."""

    outcome: Literal["refused"] = "refused"
    limit: RefusalLimit
    written: int | None = None


Outcome = BuiltOutcome | RefusedOutcome | FailedOutcome

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


def _failed_header(text: str | None) -> bytes:
    return FailedOutcome(text=text).model_dump_json().encode()


def _longest_fitting_suffix(text: str) -> str:
    """The longest suffix of the text whose OUTCOME header fits the bound."""
    shortest, longest = 0, len(text)
    while shortest < longest:
        middle = (shortest + longest + 1) // 2
        if len(_failed_header(text[len(text) - middle :])) <= MAXIMUM_HEADER_BYTES:
            shortest = middle
        else:
            longest = middle - 1
    return text[len(text) - shortest :]


def send_failed(connection: socket.socket, text: str | None = None) -> None:
    """Write the OUTCOME message: the build failed, with the longest fitting tail."""
    fitted = None if text is None else _longest_fitting_suffix(text)
    send_outcome(connection, FailedOutcome(text=fitted))


def send_outcome(connection: socket.socket, outcome: Outcome) -> None:
    """Write the OUTCOME message carrying the outcome as its header."""
    _send_message(connection, MessageKind.OUTCOME, outcome.model_dump_json().encode())


def send_end(connection: socket.socket, count: int, total: int) -> None:
    """Write the END record with the count of entries sent and their file bytes."""
    connection.sendall(bytes([RecordTag.END]) + END_COUNTS.pack(count, total))


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


def _receive_file(
    connection: socket.socket, parent: int, name: bytes, budget: int
) -> int:
    """Create the file below the parent, copy its payload in chunks, return its size.

    Refuses a size above the budget before the file is created.
    """
    (size,) = SIZE.unpack(_read_exactly(connection, SIZE.size))
    if size > budget:
        raise WireRefusedError
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


def receive_entries(
    connection: socket.socket, deck_descriptor: int, megabytes: int
) -> None:
    """Receive DIR and FILE entries below the borrowed deck, then a matching END.

    At most MAXIMUM_ENTRIES entries and megabytes of FILE bytes in total are accepted.
    Raises WireRefusedError on any grammar, order, limit or END fault; OSError passes
    through.
    Closes every descriptor it opened and leaves deck_descriptor open.
    """
    open_directories = [_OpenDirectory(deck_descriptor, b"")]
    count = total = 0
    try:
        while (tag := _read_exactly(connection, 1)[0]) != RecordTag.END:
            if tag not in {RecordTag.DIR, RecordTag.FILE}:
                raise WireRefusedError
            if count == MAXIMUM_ENTRIES:
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
                total += _receive_file(
                    connection,
                    parent.descriptor,
                    components[-1],
                    megabytes * MEGABYTE - total,
                )
            count += 1
        if END_COUNTS.unpack(_read_exactly(connection, END_COUNTS.size)) != (
            count,
            total,
        ):
            raise WireRefusedError
    finally:
        for directory in open_directories[1:]:
            os.close(directory.descriptor)


class TreeRefusedError(Exception):
    """The output tree breaks the step contract, the path rules or a limit."""

    def __init__(self, outcome: RefusedOutcome) -> None:
        """Carry the refusal the reply will send."""
        super().__init__(outcome.limit)
        self.outcome = outcome


_SHAPE_REFUSAL = RefusedOutcome(limit=RefusalLimit.SHAPE)
_REFUSING_OPEN_ERRORS = frozenset({errno.ELOOP, errno.EACCES, errno.ENXIO})


@dataclass(frozen=True)
class TreeEntry:
    """One entry of the output tree; a file's descriptor is open while it is yielded."""

    components: tuple[bytes, ...]
    size: int
    descriptor: int | None

    @property
    def is_directory(self) -> bool:
        """Whether the entry is a directory."""
        return self.descriptor is None


def _open_without_following(directory: int, name: bytes) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
    except OSError as error:
        if error.errno in _REFUSING_OPEN_ERRORS:
            raise TreeRefusedError(_SHAPE_REFUSAL) from error
        raise


def _tree(directory: int, parent: tuple[bytes, ...] = ()) -> Generator[TreeEntry]:
    """Walk depth-first in pre-order, names sorted as bytes, never following a link."""
    for name in sorted(os.fsencode(listed) for listed in os.listdir(directory)):
        components = (*parent, name)
        try:
            path_components(b"/".join(components))
        except WireRefusedError as error:
            raise TreeRefusedError(_SHAPE_REFUSAL) from error
        descriptor = _open_without_following(directory, name)
        try:
            status = os.fstat(descriptor)
            if stat.S_ISDIR(status.st_mode):
                yield TreeEntry(components, 0, None)
                yield from _tree(descriptor, components)
            elif stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
                yield TreeEntry(components, status.st_size, descriptor)
            else:
                raise TreeRefusedError(_SHAPE_REFUSAL)
        finally:
            os.close(descriptor)


def _meets_step_contract(step: StepKind, position: int, entry: TreeEntry) -> bool:
    """Build: DIR talk first, all else below it. Export: the one FILE deck.pdf."""
    if step is StepKind.EXPORT:
        return (
            position == 0
            and entry.components == (PDF_FILE.encode(),)
            and not (entry.is_directory)
        )
    talk = TALK_DIRECTORY.encode()
    if position == 0:
        return entry.components == (talk,) and entry.is_directory
    return entry.components[0] == talk


def _survey(root: int, step: StepKind, megabytes: int) -> None:
    """Walk the whole tree; raise TreeRefusedError unless it is within contract."""
    count = total = 0
    with closing(_tree(root)) as entries:
        for entry in entries:
            if count == MAXIMUM_ENTRIES:
                raise TreeRefusedError(RefusedOutcome(limit=RefusalLimit.ENTRIES))
            if not _meets_step_contract(step, count, entry):
                raise TreeRefusedError(_SHAPE_REFUSAL)
            count += 1
            total += entry.size
    if count == 0:
        raise TreeRefusedError(_SHAPE_REFUSAL)
    if total > megabytes * MEGABYTE:
        raise TreeRefusedError(
            RefusedOutcome(
                limit=RefusalLimit.MEGABYTES,
                written=(total + MEGABYTE - 1) // MEGABYTE,
            )
        )


def _send_entry(connection: socket.socket, entry: TreeEntry) -> None:
    path = b"/".join(entry.components)
    tag = RecordTag.DIR if entry.descriptor is None else RecordTag.FILE
    connection.sendall(bytes([tag]) + LENGTH.pack(len(path)) + path)
    if entry.descriptor is None:
        return
    connection.sendall(SIZE.pack(entry.size))
    remaining = entry.size
    while remaining:
        chunk = os.read(entry.descriptor, min(remaining, CHUNK_BYTES))
        if not chunk:
            raise WireRefusedError
        connection.sendall(chunk)
        remaining -= len(chunk)


def send_tree_reply(
    connection: socket.socket, root: int, step: StepKind, megabytes: int
) -> None:
    """Answer built with the tree's entries and END, or refused and END.

    The whole tree is walked and judged before the first byte is sent; sends block.
    """
    try:
        _survey(root, step, megabytes)
    except TreeRefusedError as refusal:
        send_outcome(connection, refusal.outcome)
        send_end(connection, 0, 0)
        return
    send_outcome(connection, BuiltOutcome())
    count = total = 0
    with closing(_tree(root)) as entries:
        for entry in entries:
            _send_entry(connection, entry)
            count += 1
            total += entry.size
    send_end(connection, count, total)
