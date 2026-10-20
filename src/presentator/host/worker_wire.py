"""The wire child's entry: the idle role takes a HELLO or STEP, upload answers."""

import argparse
import socket
import sys
from collections.abc import Sequence
from enum import IntEnum, StrEnum

from presentator.adapters.build_wire import (
    EmptyHeader,
    WireRefusedError,
    read_end,
    read_record,
    read_request,
    send_status,
    write_record,
)


class WireRole(StrEnum):
    """What a wire child does with its connection."""

    IDLE = "idle"
    UPLOAD = "upload"


class WireExit(IntEnum):
    """The exit statuses the controller accepts from a wire child."""

    CLOSED = 3
    ANSWERED = 4
    ACCEPTED = 5
    UPLOADED = 6


KEPT_LIFE_EXITS = frozenset({WireExit.CLOSED, WireExit.ANSWERED})


def _idle(connection: socket.socket, record_fd: int) -> WireExit:
    request = read_request(connection)
    if isinstance(request, EmptyHeader):
        send_status(connection)
        return WireExit.ANSWERED
    write_record(record_fd, request)
    return WireExit.ACCEPTED


def _upload(connection: socket.socket, record_fd: int) -> WireExit:
    read_record(record_fd)
    send_status(connection)
    read_end(connection)
    return WireExit.UPLOADED


_ROLES = {WireRole.IDLE: _idle, WireRole.UPLOAD: _upload}


def main(argv: Sequence[str]) -> int:
    """Run the role: idle answers or accepts, upload decodes the record and answers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", type=WireRole, choices=list(WireRole), required=True)
    parser.add_argument("--connection-fd", type=int, required=True)
    parser.add_argument("--record-fd", type=int, required=True)
    arguments = parser.parse_args(argv)
    with socket.socket(fileno=arguments.connection_fd) as connection:
        try:
            return _ROLES[arguments.role](connection, arguments.record_fd)
        except (WireRefusedError, OSError):
            return WireExit.CLOSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
