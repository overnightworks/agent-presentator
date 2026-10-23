"""Lifetime ownership for private co-presenter operations."""

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock

from presentator.application.copresenter_prompt import user_message
from presentator.application.spoken_answer import spoken_events, spoken_language
from presentator.contracts.copresenter import (
    AccessRefusal,
    AnswerEvent,
    AskedDeck,
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    PersonToken,
    Question,
)
from presentator.contracts.decks import is_a_plain_folder_name
from presentator.ports.clock import Clock
from presentator.ports.copresenter import (
    AgentAnswers,
    DeckTexts,
    PersonAccess,
    PrivateHearing,
    SentenceBoundaries,
    Speaking,
    SpeechHearing,
)
from presentator.ports.decks import DeckStore, SourceStore

type CoPresenterHearing = PrivateHearing

_LOGGER = logging.getLogger(__name__)
_MILLISECONDS = 1000


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedAsk:
    """An ask whose person's token is in hand, and when it began."""

    asked: AskedDeck
    person: PersonToken
    started: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CoPresenterUse:
    """Use at most one private operation and close every operation it opens."""

    answers: AgentAnswers
    speech: SpeechHearing
    decks: DeckStore
    sources: SourceStore
    texts: DeckTexts
    boundaries: SentenceBoundaries
    speaking: Speaking
    person_access: PersonAccess
    monotonic: Callable[[], float] = time.monotonic

    def asked_deck(self, slug: str, *, viewer: str) -> AskedDeck | None:
        """The viewer's own built deck and its source, or nothing for any other."""
        if not is_a_plain_folder_name(slug):
            return None
        deck = self.decks.get(slug)
        if deck is None or not deck.is_owned_by(viewer) or deck.build is None:
            return None
        source = next(
            (item for item in self.sources.all() if item.id == deck.source_id), None
        )
        return None if source is None else AskedDeck(deck=deck, source=source)

    async def readiness(self) -> CoPresenterReadiness:
        """Return speech's narrow readiness report."""
        return await self.speech.readiness()

    async def prepare(
        self, asked: AskedDeck, session_id: str
    ) -> PreparedAsk | AccessRefusal:
        """Fetch the session's fresh person token, before anything is streamed.

        Raises CoPresenterUnavailableError when the token source fails.
        """
        started = self.monotonic()
        person = await self.person_access.fresh(session_id)
        if isinstance(person, AccessRefusal):
            return person
        return PreparedAsk(asked=asked, person=person, started=started)

    @asynccontextmanager
    async def answer(
        self, prepared: PreparedAsk, question: Question
    ) -> AsyncGenerator[AsyncIterator[AnswerEvent]]:
        """Stream the spoken answer for as long as its caller reads it."""
        deck_text = await asyncio.to_thread(
            self.texts.text_of, prepared.asked.deck, prepared.asked.source
        )
        if deck_text is None:
            raise CoPresenterUnavailableError
        language = spoken_language(question.language, deck_text.language)

        async def person_token() -> str:  # NOSONAR(S7503) TokenSource is an async callable
            return prepared.person.value

        async with self.answers.answer(
            message=user_message(question, deck_text), person_token=person_token
        ) as texts:
            events = spoken_events(texts, self.boundaries, self.speaking, language)
            async with aclosing(self._timed(events, prepared.started)) as timed:
                yield timed

    async def _timed(
        self, events: AsyncIterator[AnswerEvent], started: float
    ) -> AsyncGenerator[AnswerEvent]:
        first_audio_pending = True
        async for event in events:
            if first_audio_pending and isinstance(event, Audio):
                first_audio_pending = False
                elapsed = self.monotonic() - started
                _LOGGER.info("co-presenter first audio: ms=%d", elapsed * _MILLISECONDS)
            yield event

    @asynccontextmanager
    async def hear(self, language: str) -> AsyncGenerator[PrivateHearing]:
        """Close and await the single private hearing child on every exit."""
        async with self.speech.hear(language) as hearing:
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
    _guard: AbstractContextManager[bool]

    def release(self) -> None:
        """Free the session if this ticket still holds it; later calls do nothing."""
        with self._guard:
            if self._answering.get(self.session_id) is self:
                del self._answering[self.session_id]


@dataclass(slots=True)
class AskLimiter:
    """Admit one streaming answer at a time and a bounded number per window.

    A session leaves no record once its asks left the window and nothing of it
    is streaming, so a signed-out session cannot make this grow.

    Safe to call from any thread: every check and update runs under one lock,
    so concurrent asks cannot interleave between them.
    """

    clock: Clock
    limits: AskLimits = field(default_factory=AskLimits)
    _asked: dict[str, deque[datetime]] = field(
        default_factory=dict[str, deque[datetime]]
    )
    _answering: dict[str, AskTicket] = field(default_factory=dict[str, AskTicket])
    guard: AbstractContextManager[bool] = field(default_factory=Lock)

    def admit(self, session_id: str) -> AskTicket | RetryAfter:
        """Count the question and hand out its ticket, or say when to retry."""
        with self.guard:
            return self._admit(session_id)

    def _admit(self, session_id: str) -> AskTicket | RetryAfter:
        now = self.clock.now()
        self._forget_idle_sessions(now)
        if session_id in self._answering:
            return RetryAfter(self.limits.busy_retry_seconds)
        asked = self._asked.setdefault(session_id, deque())
        if len(asked) >= self.limits.per_window:
            freed_at = asked[0] + self.limits.window
            return RetryAfter(max(1, math.ceil((freed_at - now).total_seconds())))
        asked.append(now)
        ticket = AskTicket(session_id, self._answering, self.guard)
        self._answering[session_id] = ticket
        return ticket

    def _forget_idle_sessions(self, now: datetime) -> None:
        oldest_counted = now - self.limits.window
        for session_id, asked in tuple(self._asked.items()):
            while asked and asked[0] <= oldest_counted:
                asked.popleft()
            if not asked and session_id not in self._answering:
                del self._asked[session_id]
