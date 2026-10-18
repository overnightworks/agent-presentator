"""The application owns each private co-presenter operation it starts."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_a_released_old_ticket_cannot_free_the_slot_of_a_newer_answer() -> None:
    limiter = AskLimiter(clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)))
    old = await limiter.admit("session")
    assert isinstance(old, AskTicket)
    await old.release()
    newer = await limiter.admit("session")
    assert isinstance(newer, AskTicket)

    await old.release()

    assert await limiter.admit("session") == RetryAfter(1)


@pytest.mark.anyio
async def test_sessions_that_asked_once_and_went_quiet_leave_no_record() -> None:
    clock = FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC))
    limiter = AskLimiter(clock=clock)
    for name in ("quiet-one", "quiet-two"):
        ticket = await limiter.admit(name)
        assert isinstance(ticket, AskTicket)
        await ticket.release()
    clock.advance(limiter.limits.window + timedelta(seconds=1))

    assert isinstance(await limiter.admit("late"), AskTicket)

    assert "quiet-one" not in repr(limiter)
    assert "quiet-two" not in repr(limiter)


@pytest.mark.anyio
async def test_two_concurrent_asks_of_one_session_admit_exactly_one() -> None:
    limiter = AskLimiter(clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)))

    outcomes = await asyncio.gather(limiter.admit("session"), limiter.admit("session"))

    admitted = [outcome for outcome in outcomes if isinstance(outcome, AskTicket)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, RetryAfter)]
    assert len(admitted) == 1
    assert refused == [RetryAfter(1)]
