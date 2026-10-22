"""The sweep to PID 1 and the watchdog that bounds a stalled controller."""

import errno
import faulthandler
import fcntl
import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pytest

from presentator.host.worker_termination import (
    FaultHandlerWatchdog,
    SweepEffects,
    list_process_ids,
    reap_children,
    sweep,
)

DEADLINE = 4.0
LEFTOVER = "4711"
STDERR_DESCRIPTOR = 2


@dataclass
class FakeProcessTable:
    """A process table whose clock moves only when the sweep sleeps."""

    processes: list[str] = field(default_factory=lambda: ["1"])
    leftover_dies_at_round: int | None = None
    kill_error: OSError | None = None
    listing_error: OSError | None = None
    now: float = 0.0
    listing_takes: float = 0.0
    sleeps: list[float] = field(default_factory=list[float])
    rounds: int = 0

    def kill_all(self) -> None:
        self.rounds += 1
        if self.rounds == self.leftover_dies_at_round:
            self.processes = ["1"]
        if self.kill_error is not None:
            raise self.kill_error

    def list_process_ids(self) -> Sequence[str]:
        if self.listing_error is not None:
            raise self.listing_error
        self.now += self.listing_takes
        return self.processes

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def effects(self, reap: Callable[[], None] = lambda: None) -> SweepEffects:
        return SweepEffects(
            self.kill_all, reap, self.list_process_ids, lambda: self.now, self.sleep
        )


SWEEP_OUTCOMES = {
    "only PID 1 is quiet at once": (FakeProcessTable(), 1),
    "a kill with no target is nothing to signal": (
        FakeProcessTable(kill_error=ProcessLookupError(errno.ESRCH, "no process")),
        1,
    ),
    "a leftover the kill removes is gone within the bound": (
        FakeProcessTable(processes=["1", LEFTOVER], leftover_dies_at_round=3),
        3,
    ),
}


@pytest.mark.parametrize(
    ("table", "rounds"), SWEEP_OUTCOMES.values(), ids=SWEEP_OUTCOMES.keys()
)
def test_the_sweep_is_quiet_once_only_pid_one_is_left(
    table: FakeProcessTable, rounds: int
) -> None:
    assert sweep(table.effects(), DEADLINE)
    assert (table.rounds, len(table.sleeps)) == (rounds, rounds - 1)


def test_a_leftover_that_stays_ends_the_sweep_at_its_deadline() -> None:
    table = FakeProcessTable(processes=["1", LEFTOVER])

    assert not sweep(table.effects(), DEADLINE)
    assert table.now == pytest.approx(DEADLINE)


def test_a_round_comes_every_fifty_milliseconds_and_the_last_wait_is_clipped() -> None:
    table = FakeProcessTable(processes=["1", LEFTOVER])
    deadline = 0.12

    assert not sweep(table.effects(), deadline)

    assert table.sleeps == pytest.approx([0.05, 0.05, 0.02])
    assert table.now == pytest.approx(deadline)
    assert table.rounds == len(table.sleeps)


LATE_TABLES = {
    "entered after its deadline": FakeProcessTable(now=5.0),
    "a listing that crosses the deadline": FakeProcessTable(
        now=3.99, listing_takes=0.26
    ),
}


@pytest.mark.parametrize("table", LATE_TABLES.values(), ids=LATE_TABLES.keys())
def test_only_pid_one_left_after_the_deadline_is_not_quiet(
    table: FakeProcessTable,
) -> None:
    assert not sweep(table.effects(), DEADLINE)


LISTING_ENDS_AT_DEADLINE = {
    "a leftover": (FakeProcessTable(processes=["1", LEFTOVER], now=3.75), False),
    "only PID 1": (FakeProcessTable(now=3.75), True),
}


@pytest.mark.parametrize(
    ("table", "quiet"),
    LISTING_ENDS_AT_DEADLINE.values(),
    ids=LISTING_ENDS_AT_DEADLINE.keys(),
)
def test_a_round_entered_before_the_deadline_is_judged_by_its_own_listing(
    table: FakeProcessTable, *, quiet: bool
) -> None:
    table.listing_takes = 0.25

    assert sweep(table.effects(), DEADLINE) is quiet
    assert table.rounds == 1


def test_a_sweep_entered_exactly_at_its_deadline_runs_no_round() -> None:
    table = FakeProcessTable(now=DEADLINE)

    assert not sweep(table.effects(), DEADLINE)
    assert table.rounds == 0


def test_a_listing_error_is_not_quiet() -> None:
    table = FakeProcessTable(listing_error=PermissionError(errno.EACCES, "denied"))

    assert not sweep(table.effects(), DEADLINE)


def test_a_kill_error_other_than_no_target_ends_the_sweep_with_its_error() -> None:
    effects = FakeProcessTable(
        kill_error=PermissionError(errno.EPERM, "refused")
    ).effects()

    with pytest.raises(PermissionError):
        sweep(effects, DEADLINE)


def test_the_listing_yields_only_numeric_entries(tmp_path: Path) -> None:
    for name in ("1", "42", "self", "sys", "cpuinfo"):
        (tmp_path / name).mkdir()

    assert sorted(list_process_ids(tmp_path)) == ["1", "42"]


def spawn_exited_child() -> subprocess.Popen[bytes]:
    child = subprocess.Popen(["/bin/true"])
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    return child


def unreaped_children() -> None:
    os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)


@pytest.fixture
def owns_every_child() -> Iterator[list[subprocess.Popen[bytes]]]:
    with pytest.raises(ChildProcessError):
        unreaped_children()
    children: list[subprocess.Popen[bytes]] = []
    try:
        yield children
    finally:
        for child in children:
            child.kill()
            child.wait()


def test_reaping_collects_every_exited_child_and_stops_at_none(
    owns_every_child: list[subprocess.Popen[bytes]],
) -> None:
    owns_every_child.extend(spawn_exited_child() for _ in range(2))

    reap_children()

    with pytest.raises(ChildProcessError):
        unreaped_children()


def test_reaping_leaves_a_running_child_alone(
    owns_every_child: list[subprocess.Popen[bytes]],
) -> None:
    running = subprocess.Popen(["/bin/sleep", "30"])
    owns_every_child.append(running)

    reap_children()

    assert running.poll() is None


def test_a_round_whose_kill_has_no_target_still_reaps_and_lists(
    owns_every_child: list[subprocess.Popen[bytes]],
) -> None:
    owns_every_child.append(spawn_exited_child())
    table = FakeProcessTable(kill_error=ProcessLookupError(errno.ESRCH, "none"))

    assert sweep(table.effects(reap_children), DEADLINE)

    with pytest.raises(ChildProcessError):
        unreaped_children()


@dataclass
class FaultHandlerCalls:
    """What the faulthandler timer was asked."""

    armed: list[tuple[float, bool, IO[str]]] = field(
        default_factory=list[tuple[float, bool, IO[str]]]
    )
    cancelled: int = 0


@pytest.fixture
def fake_faulthandler(monkeypatch: pytest.MonkeyPatch) -> Iterator[FaultHandlerCalls]:
    calls = FaultHandlerCalls()

    def arm(timeout: float, **options: bool | IO[str]) -> None:
        exits, file = options["exit"], options["file"]
        assert isinstance(exits, bool)
        assert not isinstance(file, bool)
        calls.armed.append((timeout, exits, file))

    def cancel() -> None:
        calls.cancelled += 1

    monkeypatch.setattr(faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", cancel)
    yield calls
    for _timeout, _exit, file in calls.armed:
        file.close()


def open_descriptors() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


def test_the_watchdog_opens_and_arms_nothing_until_it_is_armed(
    fake_faulthandler: FaultHandlerCalls,
) -> None:
    before = open_descriptors()

    FaultHandlerWatchdog()

    assert (open_descriptors(), fake_faulthandler.armed) == (before, [])


def test_arming_exits_on_expiry_through_a_non_blocking_copy_of_stderr(
    fake_faulthandler: FaultHandlerCalls,
) -> None:
    FaultHandlerWatchdog().arm(3.5)

    [(remaining, exits, file)] = fake_faulthandler.armed
    assert (remaining, exits) == (3.5, True)
    assert fcntl.fcntl(file.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK
    assert file.fileno() != STDERR_DESCRIPTOR


def test_a_second_arm_replaces_the_timer_through_the_same_descriptor(
    fake_faulthandler: FaultHandlerCalls,
) -> None:
    watchdog = FaultHandlerWatchdog()
    watchdog.arm(5.0)
    watchdog.arm(2.0)

    [(first, _, first_file), (second, _, second_file)] = fake_faulthandler.armed
    assert (first, second) == (5.0, 2.0)
    assert first_file is second_file


def test_cancelling_cancels_the_timer(fake_faulthandler: FaultHandlerCalls) -> None:
    FaultHandlerWatchdog().cancel()

    assert fake_faulthandler.cancelled == 1
