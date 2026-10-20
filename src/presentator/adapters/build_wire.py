"""The build wire's framing: read a HELLO or STEP request, write a STATUS."""

import socket
import struct
from enum import IntEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ValidationError

from presentator.contracts.decks import StepKind, is_a_plain_folder_name

MAGIC = b"PBW"
VERSION = 1
LENGTH = struct.Struct(">H")
MINIMUM_HEADER_BYTES = 2
MAXIMUM_HEADER_BYTES = 4096


class MessageKind(IntEnum):
    """The message kinds this end reads or writes."""

    HELLO = 1
    STEP = 2
    STATUS = 3


class WireRefusedError(Exception):
    """The peer sent something other than a valid HELLO or STEP."""


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


def send_status(connection: socket.socket) -> None:
    """Write the STATUS message: the preamble and the empty header."""
    header = b"{}"
    connection.sendall(
        _preamble(MessageKind.STATUS) + LENGTH.pack(len(header)) + header
    )
