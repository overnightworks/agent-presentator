"""The background poll: it keeps ticking, and never twice at once."""

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import timedelta

from presentator.host.polling import SourcePoller
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


def a_poller(refresh: HeldRefresh) -> SourcePoller:
    return SourcePoller(refresh=refresh, interval=_TIGHT)


def test_the_poll_refreshes_again_and_again_while_it_runs() -> None:
    refresh = HeldRefresh()
    refresh.release.set()

    async def poll_twice() -> bool:
        async with a_poller(refresh).polling():
            return await asyncio.to_thread(refresh.wait_for, 2)

    assert asyncio.run(poll_twice())
    assert refresh.at_once == 1


def test_a_refresh_that_outlives_its_interval_neither_doubles_nor_stops_the_next() -> (
    None
):
    refresh = HeldRefresh()

    async def poll_through_a_slow_one() -> tuple[bool, int]:
        async with a_poller(refresh).polling():
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
