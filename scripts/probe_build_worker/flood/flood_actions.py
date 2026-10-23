"""The flood deck's build-time actions, run as the build user by its build hook.

`MODE` is one of `hold`, `fork`, `fill` and `fill-after-score`. A mode that must
hand the driver a record publishes it to the life's `/tmp` and waits, bounded,
for the driver's ack: the restart that follows a flooded life wipes that tmpfs,
so the record has to be read before it. A record line is `KEY VALUE`; a failed
attempt records only the kernel's errno name, never what it touched.
"""

import errno
import os
import signal
import sys
import time
from pathlib import Path

# The life's shared 1777 tmpfs, by design: the rendezvous with the driver.
RENDEZVOUS = Path("/tmp")  # noqa: S108
PIDS_EVENTS = Path("/sys/fs/cgroup/pids.events")
OOM_SCORE = Path("/proc/self/oom_score_adj")
WAIT_SECONDS = 150
POLL_SECONDS = 0.1
CHILD_LIFETIME_SECONDS = 300
FORK_LIMIT = 4096
CHUNK_BYTES = 64 * 1024 * 1024
FILL_LIMIT_BYTES = 6 * 1024**3


def publish(name: str, content: str) -> None:
    """Write a file into the rendezvous so a reader never sees it half written."""
    pending = RENDEZVOUS / f".{name}.{os.getpid()}"
    pending.write_text(content, encoding="utf-8")
    pending.rename(RENDEZVOUS / name)


def wait_for(name: str) -> None:
    """Wait for the driver's file; give up, so the driver records not-driven."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not (RENDEZVOUS / name).exists():
        if time.monotonic() > deadline:
            sys.exit(f"the driver never published {name}")
        time.sleep(POLL_SECONDS)


def publish_record_and_wait(lines: list[str]) -> None:
    """Hand the driver the record, then hold until it has read it."""
    publish("flood.record", "".join(f"{line}\n" for line in lines))
    wait_for("flood.ack")


def errno_name(refusal: OSError) -> str:
    """The kernel's name for the refusal."""
    return errno.errorcode.get(refusal.errno or 0, "unknown")


def pids_events_max() -> int:
    """How many forks the pids controller has refused in this life."""
    for line in PIDS_EVENTS.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(" ")
        if name == "max":
            return int(value)
    return -1


def fork_until_refused() -> tuple[list[int], str]:
    """Fork sleeping children until the kernel refuses; the children and the errno."""
    children: list[int] = []
    for _ in range(FORK_LIMIT):
        try:
            child = os.fork()
        except OSError as refusal:
            return children, errno_name(refusal)
        if child == 0:
            time.sleep(CHILD_LIFETIME_SECONDS)
            os._exit(0)
        children.append(child)
    return children, "none"


def fork_bomb() -> None:
    """Exhaust pids.max, free every pid, and only then publish: exec needs a pid."""
    children, refusal = fork_until_refused()
    events = pids_events_max()
    for child in children:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
    publish_record_and_wait([f"errno {refusal}", f"events-max {events}"])


def write_score(value: str) -> str:
    """Write the value to this process's oom_score_adj: its errno name or succeeded."""
    try:
        OOM_SCORE.write_text(value, encoding="utf-8")
    except OSError as refusal:
        return errno_name(refusal)
    return "succeeded"


def lower_score() -> None:
    """Try to lower the score to 0 and then -1, publish what each write answered."""
    publish_record_and_wait(
        [f"oom-score-adj.{value} {write_score(value)}" for value in ("0", "-1")]
    )


def fill_memory() -> None:
    """Hold touched pages off the runtime heap until the OOM killer picks a process."""
    # Repeating one byte writes every page, so the pages are resident.
    _held = [
        bytearray(b"\xa5") * CHUNK_BYTES for _ in range(FILL_LIMIT_BYTES // CHUNK_BYTES)
    ]


def hold() -> None:
    """Stay in the build until the client's abandonment cancels the step."""
    wait_for("flood.release")


def main(arguments: list[str]) -> None:
    """Run one mode."""
    match arguments:
        case ["hold"]:
            hold()
        case ["fork"]:
            fork_bomb()
        case ["fill"]:
            fill_memory()
        case ["fill-after-score"]:
            lower_score()
            fill_memory()
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
