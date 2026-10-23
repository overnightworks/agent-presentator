"""Record what a life creates in the sealed worker's writable mounts (as root).

Usage: python -I upload_watcher.py WORKER_HOST_PID, with the driver holding stdin.

Reads the worker's mountinfo, classifies every writable mount, watches the
watched ones with non-recursive inotify (creates and moves in) and prints one
JSON line when armed. Once its stdin is closed and it has read the next life's
create of the worker socket, it prints one JSON line with the events up to that
create and exits. It only records; the driver judges.
"""

import ctypes
import json
import os
import select
import struct
import sys
import time
from pathlib import Path

IN_MOVED_TO = 0x80
IN_CREATE = 0x100
IN_UNMOUNT = 0x2000
IN_Q_OVERFLOW = 0x4000
IN_IGNORED = 0x8000
IN_ONLYDIR = 0x01000000
ENDED = IN_UNMOUNT | IN_IGNORED
EVENT_HEADER = struct.Struct("iIII")
BOUNDARY_ROOT = "/run/presentator-build"
BOUNDARY_NAME = "worker.sock"
BOUNDARY_SECONDS = 10
READ_BYTES = 65536

WATCHED = frozenset(
    {"/work", "/tmp", "/dev/shm", BOUNDARY_ROOT, "/dev", "/dev/mqueue"}  # noqa: S108
)
EXCLUDED_FILESYSTEMS = {
    "proc": "a kernel interface with no path create",
    "sysfs": "a kernel interface with no path create",
    "cgroup2": "a kernel interface with no path create",
    "devpts": "pty nodes appear on allocation, which inotify does not report",
}
FILE_MOUNT = "a mounted file can hold no entry"


def writable_mounts(pid: str) -> dict[str, str]:
    """The mount point and filesystem type of every mount listed `rw`."""
    mounts: dict[str, str] = {}
    for line in Path(f"/proc/{pid}/mountinfo").read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields = before.split()
        if "rw" in fields[5].split(","):
            mounts[fields[4]] = after.split()[0]
    return mounts


def classify(pid: str, mounts: dict[str, str]) -> dict[str, str]:
    """Each writable mount as `watched` or `excluded: <reason>`; else `unclassified`."""
    table: dict[str, str] = {}
    for mount_point, filesystem in mounts.items():
        if mount_point in WATCHED:
            table[mount_point] = "watched"
        elif filesystem in EXCLUDED_FILESYSTEMS:
            table[mount_point] = f"excluded: {EXCLUDED_FILESYSTEMS[filesystem]}"
        elif not Path(f"/proc/{pid}/root{mount_point}").is_dir():
            table[mount_point] = f"excluded: {FILE_MOUNT}"
        else:
            table[mount_point] = "unclassified"
    return table


def unclassified_directories(pid: str, table: dict[str, str]) -> list[str]:
    """The directories in a watched root that are not themselves classified mounts."""
    found: list[str] = []
    for mount_point, verdict in table.items():
        if verdict != "watched":
            continue
        with os.scandir(f"/proc/{pid}/root{mount_point}") as entries:
            found.extend(
                path
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
                and (path := f"{mount_point.rstrip('/')}/{entry.name}") not in table
            )
    return found


def arm(pid: str, table: dict[str, str]) -> tuple[int, dict[int, str]]:
    """One inotify descriptor, and the watch of each watched mount point."""
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.inotify_init1(os.O_CLOEXEC)
    roots: dict[int, str] = {}
    for mount_point, verdict in table.items():
        if verdict != "watched":
            continue
        path = f"/proc/{pid}/root{mount_point}".encode()
        watch = libc.inotify_add_watch(
            descriptor, path, IN_CREATE | IN_MOVED_TO | IN_ONLYDIR
        )
        if watch < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), mount_point)
        roots[watch] = mount_point
    return descriptor, roots


def decode(data: bytes) -> list[tuple[int, int, str]]:
    """The (watch, mask, name) of each event in a read."""
    events: list[tuple[int, int, str]] = []
    position = 0
    while position < len(data):
        watch, mask, _, length = EVENT_HEADER.unpack_from(data, position)
        start = position + EVENT_HEADER.size
        name = data[start : start + length].rstrip(b"\0").decode(errors="replace")
        events.append((watch, mask, name))
        position = start + length
    return events


def record(descriptor: int, roots: dict[int, str]) -> dict[str, object]:
    """Read events until stdin is closed and the next life's socket create is read."""
    created: dict[str, list[str]] = {root: [] for root in roots.values()}
    ended_at_close: list[str] = []
    ended: set[str] = set()
    overflow = boundary = stdin_closed = False
    deadline = float("inf")
    while not boundary and time.monotonic() < deadline:
        watching = [descriptor] if stdin_closed else [descriptor, sys.stdin.fileno()]
        readable, _, _ = select.select(watching, [], [], 1.0)
        if sys.stdin.fileno() in readable and not os.read(sys.stdin.fileno(), 1):
            stdin_closed = True
            deadline = time.monotonic() + BOUNDARY_SECONDS
            ended_at_close = sorted(ended)
        if descriptor not in readable:
            continue
        for watch, mask, name in decode(os.read(descriptor, READ_BYTES)):
            root = roots.get(watch, "")
            overflow = overflow or bool(mask & IN_Q_OVERFLOW)
            if mask & ENDED:
                ended.add(root)
            elif mask & (IN_CREATE | IN_MOVED_TO):
                created[root].append(name)
                if (root, name) == (BOUNDARY_ROOT, BOUNDARY_NAME):
                    boundary = True
                    break
    return {
        "boundary": boundary,
        "overflow": overflow,
        "created": created,
        "ended_at_stdin_close": ended_at_close,
        "ended_at_boundary": sorted(ended),
    }


def main(pid: str) -> None:
    """Arm, print the mount table, then record until the next life's socket."""
    table = classify(pid, writable_mounts(pid))
    directories = unclassified_directories(pid, table)
    descriptor, roots = arm(pid, table)
    sys.stdout.write(json.dumps({"mounts": table, "directories": directories}) + "\n")
    sys.stdout.flush()
    sys.stdout.write(json.dumps(record(descriptor, roots)) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
