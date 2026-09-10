"""The application owns each private co-presenter operation it starts."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from presentator.application.copresenter import CoPresenterUse
from presentator.contracts.copresenter import (
    AnswerEvent,
    CoPresenterReadiness,
    HearingTranscript,
    HearingUnavailable,
    Question,
    Text,
)
from presentator.ports.copresenter import PrivateHearing


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
                yield Text(text=question.said)
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
        assert await anext(events) == Text(text="Frage")

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
