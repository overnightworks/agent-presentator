"""The build step's launcher: it decodes the sealed record and execs Slidev."""

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from presentator.adapters.build_wire import WireRefusedError, read_record
from presentator.adapters.builds import BuildPlace, slidev_arguments

RECORD_REFUSED_EXIT_STATUS = 3


def main(
    argv: Sequence[str],
    execute: Callable[[str, list[str]], object] = os.execvp,
) -> int:
    """Exec `pnpm exec slidev` for the sealed record; a refused record never execs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-fd", type=int, required=True)
    parser.add_argument("--work", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        header = read_record(arguments.record_fd)
    except WireRefusedError:
        return RECORD_REFUSED_EXIT_STATUS
    finally:
        os.close(arguments.record_fd)
    place = BuildPlace(root=arguments.work, run=Path())
    execute(
        "pnpm",
        [
            "pnpm",
            "exec",
            "slidev",
            header.step,
            *slidev_arguments(header.step, place=place, slug=header.slug),
        ],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
