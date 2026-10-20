"""The background poll: it keeps ticking, and never twice at once."""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from presentator.host.polling import Schedule
from tests.application.fakes import PATIENCE

_TIGHT = timedelta(0)


@dataclass
class HeldRefresh:
    """A refresh a test opens and closes, so overlapping calls are visible."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Semaphore = field(
        default_factory=lambda: threading.Semaphore(0),
    )
    calls: int = 0
    at_once: int = 0
    inside: int = 0
    counting: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self) -> None:
        self._enter()
        self.started.set()
        self.release.wait(PATIENCE.total_seconds())
        with self.counting:
            self.inside -= 1
        self.finished.release()

    def wait_for(self, calls: int) -> bool:
        """Whether that many calls finished before the test ran out of patience."""
        return all(
            self.finished.acquire(timeout=PATIENCE.total_seconds())
            for _ in range(calls)
        )

    def _enter(self) -> None:
        with self.counting:
            self.calls += 1
            self.inside += 1
            self.at_once = max(self.at_once, self.inside)


def a_schedule(refresh: HeldRefresh) -> Schedule:
    return Schedule(chore=refresh, interval=_TIGHT)


def test_the_poll_refreshes_again_and_again_while_it_runs() -> None:
    refresh = HeldRefresh()
    refresh.release.set()

    async def poll_twice() -> bool:
        async with a_schedule(refresh).polling():
            return await asyncio.to_thread(refresh.wait_for, 2)

    assert asyncio.run(poll_twice())
    assert refresh.at_once == 1


def test_a_refresh_that_outlives_its_interval_neither_doubles_nor_stops_the_next() -> (
    None
):
    refresh = HeldRefresh()

    async def poll_through_a_slow_one() -> tuple[bool, int]:
        async with a_schedule(refresh).polling():
            started = await asyncio.to_thread(
                refresh.started.wait,
                PATIENCE.total_seconds(),
            )
            calls_while_the_slow_one_ran = refresh.calls
            refresh.release.set()
            finished = await asyncio.to_thread(refresh.wait_for, 2)
            return started and finished, calls_while_the_slow_one_ran

    polled, calls_while_the_slow_one_ran = asyncio.run(poll_through_a_slow_one())

    assert polled
    assert calls_while_the_slow_one_ran == 1
    assert refresh.at_once == 1


class ChoreBrokeError(RuntimeError):
    """What the chore raises."""


@dataclass
class ChoreThatFailsOnce:
    """Raises on its first call, so a test sees whether a second one comes."""

    calls: threading.Semaphore = field(
        default_factory=lambda: threading.Semaphore(0),
    )
    made: int = 0

    def __call__(self) -> None:
        self.made += 1
        self.calls.release()
        if self.made == 1:
            raise ChoreBrokeError

    def wait_for_second_call(self) -> bool:
        return all(
            self.calls.acquire(timeout=PATIENCE.total_seconds()) for _ in range(2)
        )


def test_a_chore_that_raises_is_logged_with_its_traceback_and_runs_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chore = ChoreThatFailsOnce()

    async def run_through_the_failure() -> bool:
        async with Schedule(chore=chore, interval=_TIGHT).polling():
            return await asyncio.to_thread(chore.wait_for_second_call)

    with caplog.at_level(logging.ERROR):
        ran_again = asyncio.run(run_through_the_failure())

    assert ran_again
    (failure,) = [record for record in caplog.records if record.exc_info]
    assert failure.exc_info is not None
    assert isinstance(failure.exc_info[1], ChoreBrokeError)


def test_a_schedule_leaves_no_task_running_after_its_block() -> None:
    async def run_and_leave() -> int:
        async with Schedule(chore=lambda: None, interval=_TIGHT).polling():
            pass
        return len(asyncio.all_tasks())

    assert asyncio.run(run_and_leave()) == 1
