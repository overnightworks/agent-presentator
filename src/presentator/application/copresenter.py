"""Lifetime ownership for private co-presenter operations."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from presentator.contracts.copresenter import (
    AnswerEvent,
    CoPresenterReadiness,
    Question,
)
from presentator.ports.copresenter import PrivateCoPresenter, PrivateHearing

type CoPresenterHearing = PrivateHearing


@dataclass(frozen=True, slots=True, kw_only=True)
class CoPresenterUse:
    """Use at most one private operation and close every operation it opens."""

    private: PrivateCoPresenter

    async def readiness(self) -> CoPresenterReadiness:
        """Return the private service's narrow readiness report."""
        return await self.private.readiness()

    @asynccontextmanager
    async def answer(
        self, question: Question
    ) -> AsyncGenerator[AsyncIterator[AnswerEvent]]:
        """Keep the private answer owned for exactly as long as its caller reads it."""
        async with self.private.answer(question) as events:
            yield events

    @asynccontextmanager
    async def hear(self, language: str) -> AsyncGenerator[PrivateHearing]:
        """Close and await the single private hearing child on every exit."""
        async with self.private.hear(language) as hearing:
            try:
                yield hearing
            finally:
                await hearing.aclose()
