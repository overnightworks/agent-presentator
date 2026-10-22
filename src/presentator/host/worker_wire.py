"""The wire child's entry: idle takes a HELLO or STEP, upload and send answer."""

import argparse
import os
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from presentator.adapters.build_wire import (
    EmptyHeader,
    WireRefusedError,
    read_record,
    read_request,
    receive_entries,
    send_end,
    send_failed,
    send_status,
    write_record,
)


class WireRole(StrEnum):
    """What a wire child does with its connection."""

    IDLE = "idle"
    UPLOAD = "upload"
    SEND = "send"


class WireExit(IntEnum):
    """The exit statuses the controller accepts from a wire child."""

    CLOSED = 3
    ANSWERED = 4
    ACCEPTED = 5
    UPLOADED = 6
    SENT = 7


@dataclass(frozen=True)
class WireArguments:
    """What the controller hands every wire child besides its connection."""

    record_fd: int
    work: Path
    megabytes: int


DECK_DIRECTORY = "deck"
OUTPUT_DIRECTORY = "out"
STEP_ERROR_FILE = "step.err"
KEPT_LIFE_EXITS = frozenset({WireExit.CLOSED, WireExit.ANSWERED})


def _idle(connection: socket.socket, arguments: WireArguments) -> WireExit:
    request = read_request(connection)
    if isinstance(request, EmptyHeader):
        send_status(connection)
        return WireExit.ANSWERED
    write_record(arguments.record_fd, request)
    return WireExit.ACCEPTED


def _upload(connection: socket.socket, arguments: WireArguments) -> WireExit:
    read_record(arguments.record_fd)
    deck = os.open(
        arguments.work / DECK_DIRECTORY,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        send_status(connection)
        receive_entries(connection, deck, arguments.megabytes)
    finally:
        os.close(deck)
    return WireExit.UPLOADED


def _send(connection: socket.socket, _arguments: WireArguments) -> WireExit:
    send_failed(connection)
    send_end(connection)
    return WireExit.SENT


_ROLES = {WireRole.IDLE: _idle, WireRole.UPLOAD: _upload, WireRole.SEND: _send}


def main(argv: Sequence[str]) -> int:
    """Run the role: idle answers or accepts, upload receives entries, send replies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", type=WireRole, choices=list(WireRole), required=True)
    parser.add_argument("--connection-fd", type=int, required=True)
    parser.add_argument("--record-fd", type=int, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--megabytes", type=int, required=True)
    arguments = parser.parse_args(argv)
    with socket.socket(fileno=arguments.connection_fd) as connection:
        try:
            return _ROLES[arguments.role](
                connection,
                WireArguments(arguments.record_fd, arguments.work, arguments.megabytes),
            )
        except (WireRefusedError, OSError):
            return WireExit.CLOSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
