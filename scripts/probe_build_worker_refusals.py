"""Refused uploads, in the sealed container: cell 50 (A3).

Each crafted upload of `UPLOAD_CLASSES` is sent after a valid STEP's STATUS, in a
life of its own, with a root watcher (`probe_build_worker/upload_watcher.py`, run
through `sudo -n`) recording every create in the worker's writable mounts. The
life must end as the row says, create only `deck` on `/work` and a canary
elsewhere, spawn no step, and be followed by a life that answers STATUS. One
control life that reaches a step shows the watcher and `docker top` can see.
"""

import json
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO

from probe_build_worker_harness import (
    PROBE_SOURCES,
    SERVICE,
    Report,
    compose,
    probe_client,
    refuse,
    say,
    state,
    worker,
)
from probe_build_worker_isolation import BUILD_IDENTITY, LIFE_ENDED, life_endings
from probe_build_worker_lives import (
    after_the_next_status,
    client_result,
    start_client,
    wait_for_uptime,
)
from probe_build_worker_uploads import UPLOAD_CLASSES, Ending, UploadClass

from presentator.contracts.decks import DECK_DIRECTORY, OUTPUT_DIRECTORY
from presentator.host.worker import (
    HOME_DIRECTORY,
    SOCKET_DIRECTORY,
    VITE_DIRECTORY,
    WORK_MOUNT,
)
from presentator.host.worker_serving import UPLOAD_SECONDS
from presentator.host.worker_wire import STEP_ERROR_FILE

WATCHER = PROBE_SOURCES / "upload_watcher.py"
CANARY = "cell-50-canary"
RULED_ROOTS = frozenset({WORK_MOUNT, "/tmp", "/dev/shm", SOCKET_DIRECTORY})  # noqa: S108
CONTROL_SLUG = "hello-deck"
CONTROL_CREATIONS = {
    DECK_DIRECTORY,
    HOME_DIRECTORY,
    OUTPUT_DIRECTORY,
    VITE_DIRECTORY,
    STEP_ERROR_FILE,
}
ARM_SECONDS = 30
REPORT_SECONDS = 30
CONTROL_STEP_SECONDS = 400
UPLOAD_SLACK_SECONDS = 120
PROMPT_SECONDS = 5
SAMPLE_SECONDS = 0.5
CLOSED_QUIETLY = "closed, nothing written"
CLOSED_AFTER = "closed after "
HUNG_UP = f"{Ending.HUNG_UP} "


@dataclass(frozen=True)
class Armed:
    """What the watcher printed when armed."""

    mounts: dict[str, str]
    directories: list[str]


@dataclass(frozen=True)
class WatchReport:
    """What the watcher printed at the next life's socket."""

    boundary: bool
    overflow: bool
    created: dict[str, list[str]]
    ended_at_stdin_close: list[str]
    ended_at_boundary: list[str]


@dataclass(frozen=True)
class Window:
    """What the client's life showed: its lines, and the 10001 rows `docker top` saw."""

    lines: list[str]
    samples: int
    build_rows: int


def worker_exec(*command: str) -> None:
    """Run a command in the worker; a status other than 0 is a cell not driven."""
    done = subprocess.run(
        ("docker", "exec", worker(), *command),
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    if done.returncode:
        refuse(f"cell 50 not driven: {' '.join(command)}: {done.stdout}{done.stderr}")


def host_pid() -> str:
    """The worker's pid as the host sees it."""
    done = subprocess.run(
        ("docker", "inspect", "--format", "{{.State.Pid}}", worker()),
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return done.stdout.strip()


class RootWatcher:
    """The root process that records creates; this class only starts and ends it."""

    def __init__(self) -> None:
        """Start the watcher with `sudo -n` and read the line it prints when armed."""
        self.process = subprocess.Popen(
            ("sudo", "-n", sys.executable, "-I", str(WATCHER), host_pid()),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        stdout = self.process.stdout
        if stdout is None or not _readable(stdout, ARM_SECONDS):
            self.process.kill()
            refuse("cell 50 not driven: the watcher never armed (sudo -n?)")
        line = stdout.readline()
        if not line:
            refuse("cell 50 not driven: the watcher ended before it armed")
        self.armed = Armed(**json.loads(line))

    def finish(self) -> WatchReport:
        """Close its stdin, so it ends at the next life's socket; read its report."""
        try:
            stdout, _ = self.process.communicate(timeout=REPORT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            refuse("cell 50 not driven: the watcher gave no report")
        if not stdout.strip():
            refuse("cell 50 not driven: the watcher printed no report")
        watched = WatchReport(**json.loads(stdout))
        if not watched.boundary or watched.overflow:
            refuse(f"cell 50 not driven: the watcher's window is unsound: {watched}")
        return watched


def _readable(stream: IO[str], seconds: float) -> bool:
    return bool(select.select([stream], [], [], seconds)[0])


def judge_armed(armed: Armed) -> list[str]:
    """Refuse an unclassified mount or directory; the roots to hold a canary."""
    unclassified = [
        mount for mount, verdict in armed.mounts.items() if verdict == "unclassified"
    ]
    if unclassified or armed.directories:
        refuse(f"unclassified at arming: {unclassified} {armed.directories}")
    watched = {mount for mount, verdict in armed.mounts.items() if verdict == "watched"}
    if not watched.issuperset(RULED_ROOTS):
        refuse(f"the ruled roots are not all watched: {sorted(watched)}")
    return sorted(watched - {WORK_MOUNT})


def make_canaries(roots: list[str]) -> None:
    """Create and remove one named file in each root, so its watch is shown armed."""
    for root in roots:
        worker_exec("touch", f"{root}/{CANARY}")
        worker_exec("rm", f"{root}/{CANARY}")


def build_user_rows() -> int | None:
    """How many processes of the build user `docker top` lists; None when it fails."""
    done = subprocess.run(
        ("docker", "top", worker(), "-eo", "pid,uid"),
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    if done.returncode:
        return None
    rows = (line.split() for line in done.stdout.splitlines()[1:])
    return sum(1 for row in rows if row[1:] == [BUILD_IDENTITY])


def watch_client(client: "subprocess.Popen[str]", seconds: float) -> Window:
    """Sample `docker top` from the client's start to its end, then take its lines."""
    deadline = time.monotonic() + seconds
    samples = build_rows = 0
    while True:
        rows = build_user_rows()
        if rows is not None:
            samples += 1
            build_rows += rows
        if client.poll() is not None or time.monotonic() > deadline:
            break
        time.sleep(SAMPLE_SECONDS)
    return Window(client_result(client, PROMPT_SECONDS), samples, build_rows)


def newest_life_end() -> str:
    """The value of the newest `life ended:` line in the worker's log."""
    log = compose("logs", "--no-color", SERVICE)
    ended = [line for line in log.splitlines() if LIFE_ENDED in line]
    return ended[-1].rpartition(LIFE_ENDED)[2].strip() if ended else ""


def ends_as_the_row_says(row: UploadClass, line: str, finished: datetime) -> bool:
    """Whether the client's one line is the row's ending."""
    if row.ending is Ending.REFUSED:
        return line == CLOSED_QUIETLY
    if row.ending is Ending.HUNG_UP:
        if not line.startswith(HUNG_UP):
            return False
        hung_up = datetime.fromtimestamp(float(line.removeprefix(HUNG_UP)), UTC)
        return 0 <= (finished - hung_up).total_seconds() < PROMPT_SECONDS
    if not line.startswith(CLOSED_AFTER):
        return False
    seconds = float(line.removeprefix(CLOSED_AFTER))
    if row.ending is Ending.DRIPPED:
        return UPLOAD_SECONDS - 1 <= seconds <= UPLOAD_SECONDS + PROMPT_SECONDS
    return seconds <= PROMPT_SECONDS


@dataclass(frozen=True)
class Life:
    """A life prepared and ended: its watcher, the roots with canaries, its finish."""

    window: Window
    watcher: RootWatcher
    roots: list[str]
    finished: datetime
    endings_before: int


def drive_life(client_arguments: tuple[str, ...], seconds: float) -> Life:
    """One life: arm the watcher, the canaries, the client, and the next STATUS."""
    probe_client("hello")
    wait_for_uptime()
    restarts_before = int(state()[1])
    endings_before = life_endings()
    watcher = RootWatcher()
    roots = judge_armed(watcher.armed)
    make_canaries(roots)
    window = watch_client(start_client(*client_arguments), seconds)
    finished = after_the_next_status(restarts_before)
    return Life(window, watcher, roots, finished, endings_before)


def prove_control_life() -> str:
    """A step reaches 10001: the watcher sees its creates and `docker top` its row."""
    life = drive_life(("outcome", CONTROL_SLUG), CONTROL_STEP_SECONDS)
    created = life.watcher.finish().created
    if not set(created[WORK_MOUNT]).issuperset(CONTROL_CREATIONS):
        refuse(
            f"{WORK_MOUNT} saw {created[WORK_MOUNT]}, not {sorted(CONTROL_CREATIONS)}"
        )
    if not life.window.build_rows:
        refuse(f"docker top never listed {BUILD_IDENTITY}: {life.window}")
    mounts = json.dumps(life.watcher.armed.mounts, sort_keys=True)
    say(f"cell 50 mounts: {mounts}")
    rows = life.window.build_rows
    return f"control life created {sorted(CONTROL_CREATIONS)}, {rows} 10001 rows"


def prove_refused_upload(name: str, row: UploadClass) -> str:
    """One refused upload in its own life: the ending, the life end, the creates."""
    started = time.monotonic()
    life = drive_life(("upload-class", name), UPLOAD_SECONDS + UPLOAD_SLACK_SECONDS)
    if len(life.window.lines) != 1 or not ends_as_the_row_says(
        row, life.window.lines[0], life.finished
    ):
        refuse(f"{name}: the client printed {life.window.lines}, not {row.ending}")
    line = life.window.lines[0]
    if newest_life_end() != row.life_end or life_endings() != life.endings_before + 1:
        refuse(f"{name}: the life did not end as {row.life_end}")
    report = life.watcher.finish()
    created = report.created
    if created[WORK_MOUNT] != [DECK_DIRECTORY]:
        refuse(f"{name}: {WORK_MOUNT} saw {created[WORK_MOUNT]}, not its deck")
    for root in life.roots:
        if created[root] != [CANARY]:
            refuse(f"{name}: {root} saw {created[root]}, not its canary")
    if life.window.samples < 1 or life.window.build_rows:
        refuse(f"{name}: docker top {life.window} is not clean samples of no 10001")
    ended = report.ended_at_stdin_close, report.ended_at_boundary
    seconds = time.monotonic() - started
    return f"{name}: {line}; {row.life_end}; {seconds:.0f}s; ended {ended}"


def prove_refused_uploads(report: Report) -> None:
    """Cell 50: each crafted upload is refused in a life of its own, writing nothing."""
    control = prove_control_life()
    say(control)
    for name, row in UPLOAD_CLASSES.items():
        say(f"cell 50 row {prove_refused_upload(name, row)}")
    report.cell(
        "50", f"{len(UPLOAD_CLASSES)} refused uploads, one life each; {control}"
    )
