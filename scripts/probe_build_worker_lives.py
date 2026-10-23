"""Lives that end badly, in the sealed container: cells 25 C, 40, 45, 46 and 56 C.

A client that abandons the build, a deck that exhausts a bound, and a leftover of
a kept life. Each ended life is judged as the kernel and Docker record it: the
restart count read once, after the next life's STATUS; the fork refusal by
`pids.events max` risen above a baseline read before the STEP; the OOM killer by
Docker's own `oom` event; the kill timing by `State.FinishedAt`; a leftover by
the host's `/proc`. The flood deck (`probe_build_worker/flood`) takes its mode
from a file the driver publishes into the life's `/tmp` before the STEP.
"""

import select
import subprocess
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO

from probe_build_worker_harness import (
    ENTRY_FILE,
    HOLDER,
    PROBE_DIRECTORY,
    PROBE_SOURCES,
    Report,
    docker,
    probe_client,
    refuse,
    state,
    worker,
)
from probe_build_worker_isolation import (
    BUILD_IDENTITY,
    PYTHON,
    TEMPORARY,
    WIRE_IDENTITY,
    WIRE_USER,
    exec_in_worker,
    wait_for_deck,
)

from presentator.host.worker_termination import SWEEP_SECONDS

FLOOD_SLUG = "flood"
MODE_FILE = TEMPORARY / "flood.mode"
RECORD_FILE = TEMPORARY / "flood.record"
ACK_FILE = TEMPORARY / "flood.ack"
PIDS_EVENTS = "/sys/fs/cgroup/pids.events"
UPTIME_RESET_SECONDS = 10
UPTIME_DEADLINE_SECONDS = 30
RECORD_SECONDS = 240
FLOOD_SECONDS = 600
ABANDON_SECONDS = 120
FINISH_WINDOW_SECONDS = 2
LAUNCH_SLACK_SECONDS = 3
POLL_SECONDS = 0.05
RECORD_POLL_SECONDS = 0.5
HANDSHAKE_SECONDS = 120


class FloodMode(StrEnum):
    """The modes the flood deck's build hook reads from the life's /tmp."""

    HOLD = "hold"
    FORK = "fork"
    FILL = "fill"
    FILL_AFTER_SCORE = "fill-after-score"


class StepEnd(StrEnum):
    """How a flood client saw its step end; the client prints it, the driver reads."""

    FAILED_WITH_TEXT = "outcome failed with-text"
    FAILED_WITHOUT_TEXT = "outcome failed without-text"
    BUILT = "outcome built"
    REFUSED = "outcome refused"
    CLOSED = "closed"
    MALFORMED = "malformed"


ACCEPTED_FILL_ENDS = frozenset(
    {StepEnd.FAILED_WITH_TEXT, StepEnd.FAILED_WITHOUT_TEXT, StepEnd.CLOSED}
)


class Abandonment(StrEnum):
    """How the client leaves a build after END."""

    HANG_UP = "hang-up"
    HALF_CLOSE = "half-close"


def docker_time(text: str) -> datetime:
    """Docker prints nanoseconds; keep the microseconds datetime can hold."""
    stamp, _, fraction = text.removesuffix("Z").partition(".")
    return datetime.fromisoformat(f"{stamp}.{fraction[:6].ljust(6, '0')}+00:00")


def wait_for_uptime() -> None:
    """Wait until the life is ten seconds old, so Docker's restart delay resets."""
    deadline = time.monotonic() + UPTIME_DEADLINE_SECONDS
    while (datetime.now(UTC) - docker_time(state()[0])).total_seconds() < (
        UPTIME_RESET_SECONDS
    ):
        if time.monotonic() > deadline:
            refuse("the worker's life never reached its uptime reset")
        time.sleep(RECORD_POLL_SECONDS)


def begin_life_to_end(mode: FloodMode) -> int:
    """HELLO, the uptime wait, the mode into this life's /tmp; the restart count."""
    probe_client("hello")
    wait_for_uptime()
    docker("exec", worker(), "sh", "-c", f"echo {mode.value} > {MODE_FILE}")
    return int(state()[1])


def baseline_pids_events_max() -> int:
    """The refusals the pids controller has counted so far in this life."""
    counts = dict(
        line.split()
        for line in docker("exec", worker(), "cat", PIDS_EVENTS).splitlines()
    )
    return int(counts["max"])


def after_the_next_status(restarts_before: int) -> datetime:
    """The next life answers STATUS; the ended life's finish, after one restart."""
    probe_client("hello")
    finished, restarts = docker(
        "inspect", "--format", "{{.State.FinishedAt}} {{.RestartCount}}", worker()
    ).split()
    if int(restarts) != restarts_before + 1:
        refuse(
            f"the life did not end in exactly one restart: {restarts_before} {restarts}"
        )
    return docker_time(finished)


def start_client(*arguments: str, stdin: int | None = None) -> "subprocess.Popen[str]":
    """Start a client command in the holder, its output piped back."""
    interactive = ("-i",) if stdin is not None else ()
    return subprocess.Popen(
        ("docker", "exec", *interactive, HOLDER, "python",
         f"{PROBE_DIRECTORY}/{ENTRY_FILE}", "client", *arguments),
        stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def client_result(client: "subprocess.Popen[str]", seconds: int) -> list[str]:
    """The lines a finished client printed; a failed or stalled client is a refusal."""
    try:
        stdout, stderr = client.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        client.kill()
        refuse(f"the client not driven: no end within {seconds} seconds")
    if client.returncode:
        refuse(f"the client: {client.returncode} {stdout}{stderr}")
    return stdout.splitlines()


def await_record(client: "subprocess.Popen[str]") -> dict[str, str]:
    """Read the deck's record from the life's /tmp; a refused exec counts as not yet."""
    deadline = time.monotonic() + RECORD_SECONDS
    while docker_exec_status("test", "-s", str(RECORD_FILE)):
        if client.poll() is not None:
            refuse(f"the client ended before the deck's record: {client.communicate()}")
        if time.monotonic() > deadline:
            client.kill()
            refuse("the deck's record not driven: it never appeared")
        time.sleep(RECORD_POLL_SECONDS)
    record = docker("exec", worker(), "cat", str(RECORD_FILE))
    return dict(line.split(maxsplit=1) for line in record.splitlines())


def docker_exec_status(*command: str) -> int:
    """The status of a command in the worker; a refused exec counts as not yet."""
    return subprocess.run(
        ("docker", "exec", worker(), *command), check=False, capture_output=True
    ).returncode


def acknowledge() -> None:
    """Tell the deck the record was read, so it may go on."""
    docker("exec", worker(), "sh", "-c", f"echo ack > {ACK_FILE}")


def outcome_line(client: "subprocess.Popen[str]") -> StepEnd:
    """The one line the flood client printed: how its step ended."""
    lines = client_result(client, FLOOD_SECONDS)
    if len(lines) != 1 or lines[0] not in StepEnd:
        refuse(f"the flood client printed {lines}")
    return StepEnd(lines[0])


def prove_fork_bomb(report: Report) -> None:
    """Cell 40: pids.max refuses the fork bomb; the step fails; the next life serves."""
    restarts_before = begin_life_to_end(FloodMode.FORK)
    baseline = baseline_pids_events_max()
    client = start_client("outcome", FLOOD_SLUG)
    record = await_record(client)
    acknowledge()
    outcome = outcome_line(client)
    after_the_next_status(restarts_before)
    events = int(record["events-max"])
    if events <= baseline:
        refuse(f"pids.events max did not rise: {baseline} before, {events} after")
    if outcome is not StepEnd.FAILED_WITH_TEXT:
        refuse(f"the fork bomb's step answered {outcome!r}, not failed with text")
    report.cell(
        "40",
        f"pids.events max {baseline} -> {events}; refused with {record['errno']}; "
        f"{outcome}; one restart",
    )


def oom_events_between(started: float) -> int:
    """How many oom events Docker recorded for the worker since the given time."""
    events = docker(
        "events", "--since", f"{started:.3f}", "--until", f"{time.time():.3f}",
        "--filter", "event=oom", "--filter", f"container={worker()}",
        "--format", "{{.Action}}",
    )  # fmt: skip
    return len(events.splitlines())


def prove_memory_exhaustion(report: Report, mode: FloodMode, cell: str) -> None:
    """Cells 45 and 46: the OOM killer ends the life; failed or closed."""
    restarts_before = begin_life_to_end(mode)
    started = time.time() - 1
    client = start_client("outcome", FLOOD_SLUG)
    record: dict[str, str] = {}
    if mode is FloodMode.FILL_AFTER_SCORE:
        record = await_record(client)
        if record.get("oom-score-adj.-1") != "EACCES":
            client.kill()
            refuse(f"lowering the score below its floor answered {record}")
        acknowledge()
    outcome = outcome_line(client)
    after_the_next_status(restarts_before)
    if outcome not in ACCEPTED_FILL_ENDS:
        refuse(f"the flooded step answered {outcome!r}")
    if not oom_events_between(started):
        refuse("Docker recorded no oom event for the worker")
    scores = ", ".join(f"{key} {value}" for key, value in record.items())
    report.cell(
        cell, f"oom event seen; client saw {outcome!r}; {scores or 'score untouched'}"
    )


def build_user_process(pid: str) -> bool:
    """Whether the process in the worker is alive and runs as the build user."""
    status = subprocess.run(
        ("docker", "exec", worker(), "cat", f"/proc/{pid}/status"),
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    uid = [
        line.split()[1]
        for line in status.stdout.splitlines()
        if line.startswith("Uid:")
    ]
    return uid == [BUILD_IDENTITY]


def handshake_line(client: "subprocess.Popen[str]", pipe: IO[str]) -> str:
    """The client's next line; a stalled or dead client is a refusal, not a wait."""
    ready, _, _ = select.select([pipe], [], [], HANDSHAKE_SECONDS)
    line = pipe.readline().strip() if ready else ""
    if not line:
        client.kill()
        refuse(f"the client not driven: no handshake line: {client.communicate()}")
    return line


def prove_abandoned_build(report: Report, how: Abandonment) -> None:
    """Cell 25 C: a client that leaves after END gets its step killed."""
    restarts_before = begin_life_to_end(FloodMode.HOLD)
    client = start_client("abandon", FLOOD_SLUG, how.value, stdin=subprocess.PIPE)
    if client.stdout is None or client.stdin is None:
        refuse("the abandoning client has no pipes")
    if handshake_line(client, client.stdout) != "uploaded":
        refuse(f"the client did not upload: {client.communicate()}")
    wait_for_deck(client)
    step = docker("exec", worker(), "cat", str(TEMPORARY / "deck.pid")).strip()
    if not build_user_process(step):
        refuse(f"the step {step} is not alive as {BUILD_IDENTITY} before the close")
    try:
        client.stdin.write("close\n")
        client.stdin.flush()
    except BrokenPipeError:
        refuse(f"the client died before the close: {client.communicate()}")
    closed_at = float(handshake_line(client, client.stdout).split()[1])
    client_result(client, ABANDON_SECONDS)
    finished = after_the_next_status(restarts_before)
    gap = (finished - datetime.fromtimestamp(closed_at, UTC)).total_seconds()
    if not 0 <= gap < FINISH_WINDOW_SECONDS:
        refuse(f"{how.value}: the life finished {gap:.2f} s after the close")
    report.cell(
        "25",
        f"C {how.value}: step {step} running as {BUILD_IDENTITY} in B; "
        f"finished {gap:.2f} s after the close; one restart",
    )


def leave_leftover() -> int:
    """Leave a `setsid` 10002 process in the worker; its host pid, from `docker top`."""
    done = subprocess.run(
        exec_in_worker(PYTHON, "-I", "-", "fork", user=WIRE_USER),
        input=(PROBE_SOURCES / "attack" / "kernel_attacks.py").read_text(),
        capture_output=True, text=True, check=False, timeout=ABANDON_SECONDS,
    )  # fmt: skip
    if done.returncode or done.stdout.split()[-1:] != [WIRE_IDENTITY]:
        refuse(
            f"the leftover was not left: {done.returncode} {done.stdout}{done.stderr}"
        )
    listing = docker("top", worker(), "-eo", "pid,uid,args").splitlines()[1:]
    rows = [line.split(maxsplit=2) for line in listing]
    found = [
        pid
        for pid, uid, args in rows
        if uid == WIRE_IDENTITY and args.endswith("- fork")
    ]
    if len(found) != 1 or not Path(f"/proc/{found[0]}").exists():
        refuse(f"the leftover is not one live host process: {found}")
    return int(found[0])


def prove_leftover_swept(name: str, connection: tuple[str, ...]) -> float:
    """One kept-life connection; the seconds until its leftover left /proc."""
    leftover = Path(f"/proc/{leave_leftover()}")
    bound = SWEEP_SECONDS + LAUNCH_SLACK_SECONDS
    started = time.monotonic()
    client = start_client(*connection)
    while leftover.exists():
        if time.monotonic() - started > bound:
            client.kill()
            refuse(f"{name}: the leftover outlived the sweep bound of {bound} s")
        time.sleep(POLL_SECONDS)
    seconds = time.monotonic() - started
    client_result(client, ABANDON_SECONDS)
    return seconds


def prove_kept_life_leftovers(
    report: Report, connections: dict[str, tuple[str, ...]]
) -> None:
    """Cell 56 C: each kept-life sweep reaches a 10002 leftover, no restart."""
    before = state()
    probe_client("hello")
    seconds = {
        name: prove_leftover_swept(name, rest) for name, rest in connections.items()
    }
    if state() != before:
        refuse("a kept life restarted the worker")
    probe_client("hello")
    report.cell(
        "56",
        "C leftover gone from the host's /proc after "
        + ", ".join(f"{name} in {value:.2f} s" for name, value in seconds.items())
        + "; same life; next HELLO answered",
    )


def prove_lives_that_end_badly(
    report: Report, connections: dict[str, tuple[str, ...]]
) -> None:
    """Cells 25 C, 40, 45, 46 and 56 C: lives ended by abandonment or exhaustion."""
    prove_kept_life_leftovers(report, connections)
    for how in Abandonment:
        prove_abandoned_build(report, how)
    prove_fork_bomb(report)
    prove_memory_exhaustion(report, FloodMode.FILL, "45")
    prove_memory_exhaustion(report, FloodMode.FILL_AFTER_SCORE, "46")
