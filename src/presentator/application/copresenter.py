"""Lifetime ownership for private co-presenter operations."""

import math
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from presentator.contracts.copresenter import (
    AnswerEvent,
    CoPresenterReadiness,
    Question,
)
from presentator.ports.clock import Clock
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


@dataclass(frozen=True, slots=True, kw_only=True)
class AskLimits:
    """How much of the host's answer capacity one session may use."""

    per_window: int = 20
    window: timedelta = timedelta(minutes=10)
    busy_retry_seconds: int = 1


@dataclass(frozen=True, slots=True)
class RetryAfter:
    """A refused question and the whole seconds until it may be asked again."""

    seconds: int


@dataclass(eq=False, slots=True)
class AskTicket:
    """The one answer a session is allowed to stream right now."""

    session_id: str
    _answering: dict[str, "AskTicket"]

    def release(self) -> None:
        """Free the session if this ticket still holds it; later calls do nothing."""
        if self._answering.get(self.session_id) is self:
            del self._answering[self.session_id]


@dataclass(slots=True)
class AskLimiter:
    """Admit one streaming answer at a time and a bounded number per window.

    A session leaves no record once its asks left the window and nothing of it
    is streaming, so a signed-out session cannot make this grow.

    Not thread-safe: call it only from the event-loop thread, where no await
    separates a check from its update.
    """

    clock: Clock
    limits: AskLimits = field(default_factory=AskLimits)
    _asked: dict[str, deque[datetime]] = field(
        default_factory=dict[str, deque[datetime]]
    )
    _answering: dict[str, AskTicket] = field(default_factory=dict[str, AskTicket])

    def admit(self, session_id: str) -> AskTicket | RetryAfter:
        """Count the question and hand out its ticket, or say when to retry."""
        now = self.clock.now()
        self._forget_idle_sessions(now)
        if session_id in self._answering:
            return RetryAfter(self.limits.busy_retry_seconds)
        asked = self._asked.setdefault(session_id, deque())
        if len(asked) >= self.limits.per_window:
            freed_at = asked[0] + self.limits.window
            return RetryAfter(max(1, math.ceil((freed_at - now).total_seconds())))
        asked.append(now)
        ticket = AskTicket(session_id, self._answering)
        self._answering[session_id] = ticket
        return ticket

    def _forget_idle_sessions(self, now: datetime) -> None:
        oldest_counted = now - self.limits.window
        for session_id, asked in tuple(self._asked.items()):
            while asked and asked[0] <= oldest_counted:
                asked.popleft()
            if not asked and session_id not in self._answering:
                del self._asked[session_id]
