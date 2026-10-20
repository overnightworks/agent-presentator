"""The build wire's framing: read a HELLO, write a STATUS."""

import socket
import struct
from enum import IntEnum

from pydantic import BaseModel, ValidationError

MAGIC = b"PBW"
VERSION = 1
LENGTH = struct.Struct(">H")
MINIMUM_HEADER_BYTES = 2
MAXIMUM_HEADER_BYTES = 4096


class MessageKind(IntEnum):
    """The message kinds this end reads or writes."""

    HELLO = 1
    STATUS = 3


class WireRefusedError(Exception):
    """The peer sent something other than a valid HELLO."""


class EmptyHeader(BaseModel, extra="forbid", strict=True):
    """The header of a HELLO or STATUS: exactly the empty object."""


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


def read_hello(connection: socket.socket) -> None:
    """Read one HELLO preamble and header; raise WireRefusedError on anything else."""
    if _read_exactly(connection, len(_preamble(MessageKind.HELLO))) != _preamble(
        MessageKind.HELLO
    ):
        raise WireRefusedError
    (length,) = LENGTH.unpack(_read_exactly(connection, LENGTH.size))
    if not MINIMUM_HEADER_BYTES <= length <= MAXIMUM_HEADER_BYTES:
        raise WireRefusedError
    try:
        EmptyHeader.model_validate_json(_read_exactly(connection, length))
    except ValidationError as error:
        raise WireRefusedError from error


def send_status(connection: socket.socket) -> None:
    """Write the STATUS message: the preamble and the empty header."""
    header = b"{}"
    connection.sendall(
        _preamble(MessageKind.STATUS) + LENGTH.pack(len(header)) + header
    )
