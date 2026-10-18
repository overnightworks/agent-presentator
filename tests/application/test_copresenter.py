"""The application owns each private co-presenter operation it starts."""

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from presentator.application.copresenter import (
    AskLimiter,
    AskTicket,
    CoPresenterUse,
    RetryAfter,
)
from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerText,
    CoPresenterReadiness,
    HearingTranscript,
    HearingUnavailable,
    Question,
)
from presentator.ports.copresenter import PrivateHearing
from tests.application.fakes import FrozenClock


@dataclass
class FakeHearing:
    """A private hearing child that records ownership."""

    closed: int = 0
    frames: list[bytes] = field(default_factory=list[bytes])

    async def send_pcm(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def receive(self) -> HearingTranscript | HearingUnavailable | None:
        return None

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class FakePrivateCoPresenter:
    """A private port fake with observable context exits."""

    answer_exits: int = 0
    hearing_exits: int = 0
    hearing: FakeHearing = field(default_factory=FakeHearing)

    async def readiness(self) -> CoPresenterReadiness:
        return CoPresenterReadiness(
            answerer_model="canned",
            hearing_sample_rate=16_000,
            local_hearing_ready=True,
        )

    def answer(
        self,
        question: Question,
    ) -> AbstractAsyncContextManager[AsyncIterator[AnswerEvent]]:
        @asynccontextmanager
        async def operation() -> AsyncGenerator[AsyncIterator[AnswerEvent]]:
            async def events() -> AsyncIterator[AnswerEvent]:
                yield AnswerText(text=question.said)
                await asyncio.Future()

            try:
                yield events()
            finally:
                self.answer_exits += 1

        return operation()

    def hear(self, language: str) -> AbstractAsyncContextManager[PrivateHearing]:
        assert language == "de"

        @asynccontextmanager
        async def operation() -> AsyncGenerator[PrivateHearing]:
            try:
                yield self.hearing
            finally:
                self.hearing_exits += 1

        return operation()


def test_cancelling_an_answer_awaits_the_private_operation() -> None:
    asyncio.run(_cancel_answer())


async def _cancel_answer() -> None:
    private = FakePrivateCoPresenter()
    use = CoPresenterUse(private=private)

    async with use.answer(Question(said="Frage", slide=1, language=None)) as events:
        assert await anext(events) == AnswerText(text="Frage")

    assert private.answer_exits == 1


def test_ending_hearing_closes_and_awaits_the_private_child() -> None:
    asyncio.run(_end_hearing())


async def _end_hearing() -> None:
    private = FakePrivateCoPresenter()
    use = CoPresenterUse(private=private)

    async with use.hear("de") as hearing:
        await hearing.send_pcm(b"pcm")

    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1
    assert private.hearing_exits == 1


def a_limiter() -> AskLimiter:
    return AskLimiter(clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)))


def test_a_released_old_ticket_cannot_free_the_slot_of_a_newer_answer() -> None:
    limiter = a_limiter()
    old = limiter.admit("session")
    assert isinstance(old, AskTicket)
    old.release()
    newer = limiter.admit("session")
    assert isinstance(newer, AskTicket)

    old.release()

    assert limiter.admit("session") == RetryAfter(1)


def test_sessions_that_asked_once_and_went_quiet_leave_no_record() -> None:
    clock = FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC))
    limiter = AskLimiter(clock=clock)
    for name in ("quiet-one", "quiet-two"):
        ticket = limiter.admit(name)
        assert isinstance(ticket, AskTicket)
        ticket.release()
    clock.advance(limiter.limits.window + timedelta(seconds=1))

    assert isinstance(limiter.admit("late"), AskTicket)

    assert "quiet-one" not in repr(limiter)
    assert "quiet-two" not in repr(limiter)


RACE_WINDOW_SECONDS = 0.02
THREADS = 8


class LingeringAnswering(dict[str, AskTicket]):
    """An answering table that lingers around each check and change, widening races."""

    def __contains__(self, key: object) -> bool:
        """Report membership, then linger before the caller acts on it."""
        found = super().__contains__(key)
        time.sleep(RACE_WINDOW_SECONDS)
        return found

    def __delitem__(self, key: str) -> None:
        """Linger before the change, then make it."""
        time.sleep(RACE_WINDOW_SECONDS)
        super().__delitem__(key)


def at_the_same_instant[Result](action: Callable[[], Result]) -> list[Result]:
    barrier = threading.Barrier(THREADS)

    def start_together() -> Result:
        barrier.wait()
        return action()

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(start_together) for _ in range(THREADS)]
        return [future.result() for future in futures]


def test_simultaneous_asks_of_one_session_on_many_threads_admit_exactly_one() -> None:
    limiter = AskLimiter(
        clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)),
        _answering=LingeringAnswering(),
    )

    outcomes = at_the_same_instant(lambda: limiter.admit("session"))

    admitted = [outcome for outcome in outcomes if isinstance(outcome, AskTicket)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, RetryAfter)]
    assert len(admitted) == 1
    assert refused == [RetryAfter(1)] * (THREADS - 1)


def test_simultaneous_releases_of_one_ticket_free_the_slot_exactly_once() -> None:
    limiter = AskLimiter(
        clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)),
        _answering=LingeringAnswering(),
    )
    ticket = limiter.admit("session")
    assert isinstance(ticket, AskTicket)

    at_the_same_instant(ticket.release)

    assert isinstance(limiter.admit("session"), AskTicket)
