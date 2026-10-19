"""The wire child's entry: it closes its connection unread and exits CLOSED."""

import argparse
import os
import sys
from collections.abc import Sequence
from enum import IntEnum


class WireExit(IntEnum):
    """The exit statuses the controller accepts from a wire child."""

    CLOSED = 3


def main(argv: Sequence[str]) -> int:
    """Close the connection named by --connection-fd without reading it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-fd", type=int, required=True)
    os.close(parser.parse_args(argv).connection_fd)
    return WireExit.CLOSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
