"""What a life left behind, read in the next life (`python -I - PATH...`, on stdin).

Prints one JSON object: the real uid of every process that is not root's, the
SysV objects each table lists, and the markers that exist.
"""

import json
import sys
from pathlib import Path

SYSV_TABLES = ("shm", "msg", "sem")
TABLE_HEADER_LINES = 1


def process_owners() -> list[str]:
    """The real uid of every process in this namespace."""
    owners: list[str] = []
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            lines = status.read_text().splitlines()
        except OSError:
            continue
        owners.extend(line.split()[1] for line in lines if line.startswith("Uid:"))
    return owners


def sysv_objects() -> dict[str, int]:
    """The objects each SysV table lists."""
    return {
        table: len(Path(f"/proc/sysvipc/{table}").read_text().splitlines())
        - TABLE_HEADER_LINES
        for table in SYSV_TABLES
    }


def main(markers: list[str]) -> None:
    """Print what this life carries."""
    sys.stdout.write(
        json.dumps(
            {
                "owners": process_owners(),
                "sysv": sysv_objects(),
                "markers": [path for path in markers if Path(path).exists()],
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
