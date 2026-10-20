"""The wire child's entry: the idle role answers a HELLO and closes."""

import argparse
import socket
import sys
from collections.abc import Sequence
from enum import IntEnum

from presentator.adapters.build_wire import WireRefusedError, read_hello, send_status


class WireExit(IntEnum):
    """The exit statuses the controller accepts from a wire child."""

    CLOSED = 3
    ANSWERED = 4


KEPT_LIFE_EXITS = frozenset({WireExit.CLOSED, WireExit.ANSWERED})


def main(argv: Sequence[str]) -> int:
    """Answer a HELLO on --connection-fd with STATUS; close on anything else."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-fd", type=int, required=True)
    with socket.socket(fileno=parser.parse_args(argv).connection_fd) as connection:
        try:
            read_hello(connection)
            send_status(connection)
        except (WireRefusedError, OSError):
            return WireExit.CLOSED
    return WireExit.ANSWERED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
