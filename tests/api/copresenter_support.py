"""The co-presenter talk host's shared fakes, values and builders."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME

from presentator.adapters.sentences import split_sentences
from presentator.api.copresenter import CoPresenterSurface
from presentator.application.copresenter import AskLimiter, CoPresenterUse
from presentator.contracts.copresenter import (
    AccessRefusal,
    AskedDeck,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    DeckText,
    HearingTranscript,
    PersonToken,
    SlideText,
)
from presentator.contracts.decks import Deck, Source
from tests.api.lobby import (
    A_BROWSERS_HEADERS,
    A_TALK_ADDRESS,
    A_TALK_SOCKET_ADDRESS,
    ADMIN,
    NOW,
    SOURCE_ID,
    Lobby,
    a_browser_at,
    a_crowd,
    a_talk_app,
)
from tests.api.test_deck_page import a_build, a_deck_store
from tests.application.fakes import (
    FakeDeckStore,
    FakeSourceStore,
    FakeUserStore,
    FrozenClock,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Mapping,
    )

    from fastapi.testclient import TestClient
    from httpx2 import Response

    from presentator.ports.copresenter import PrivateHearing, TokenSource

TALK_ORIGIN = A_TALK_ADDRESS
A_DECK = "kundenfeedback"
QUESTION = {"said": "Was ist wichtig?", "slide": 2, "language": "de", "deck": A_DECK}
A_SOURCE = Source(id=SOURCE_ID, name="s", url="u", ref="main", owner_id=ADMIN)


def the_admins_decks() -> FakeDeckStore:
    return a_deck_store(slug=A_DECK, built=a_build())


A_DECK_TEXT = DeckText(
    title="Kundenfeedback",
    language="de",
    slides=(
        SlideText(number=1, title="Start", body="Willkommen", notes=""),
        SlideText(number=2, title="Wichtig", body="Der Kern", notes="Betonen"),
    ),
)
ASKED = AskedDeck(deck=the_admins_decks().kept[A_DECK], source=A_SOURCE)


HEARING = f"{A_TALK_SOCKET_ADDRESS}/copresenter/hear?language=de"
POLICY_VIOLATION = 1008
UNSUPPORTED_DATA = 1003
INTERNAL_ERROR = 1011


@dataclass
class RecordingHearing:
    """The private hearing child whose use the route makes observable."""

    frames: list[bytes] = field(default_factory=list[bytes])
    closed: int = 0
    events: list[HearingTranscript | RuntimeError | None] = field(
        default_factory=lambda: [HearingTranscript(text="gehört", final=True)]
    )
    _frame_ready: asyncio.Queue[None] | None = field(default=None, init=False)

    async def send_pcm(self, frame: bytes) -> None:
        self.frames.append(frame)
        if self._frame_ready is None:
            self._frame_ready = asyncio.Queue()
        self._frame_ready.put_nowait(None)

    async def receive(self) -> HearingTranscript | None:
        if self._frame_ready is None:
            self._frame_ready = asyncio.Queue()
        await self._frame_ready.get()
        if self.events:
            event = self.events.pop(0)
            if isinstance(event, RuntimeError):
                raise event
            return event
        await asyncio.Future()
        return None

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class RecordingSpeech:
    """The speech service's hearing side: readiness and the one hearing child."""

    readiness_calls: int = 0
    hearing_opens: int = 0
    hearing: RecordingHearing = field(default_factory=RecordingHearing)
    readiness_unavailable: bool = False
    hearing_unavailable: bool = False
    grouped_hearing_context: bool = False

    async def readiness(self) -> CoPresenterReadiness:
        self.readiness_calls += 1
        if self.readiness_unavailable:
            raise CoPresenterUnavailableError
        return CoPresenterReadiness(
            hearing_sample_rate=16_000,
            local_hearing_ready=True,
        )

    def hear(self, language: str) -> AbstractAsyncContextManager[PrivateHearing]:
        @asynccontextmanager
        async def operation() -> AsyncGenerator[PrivateHearing]:
            assert language == "de"
            self.hearing_opens += 1
            if self.hearing_unavailable:
                raise CoPresenterUnavailableError
            if self.grouped_hearing_context:
                async with anyio.create_task_group():
                    yield self.hearing
                return
            yield self.hearing

        return operation()


@dataclass
class RecordingAnswers:
    """The agent service's text, behind the real application use case."""

    messages: list[str] = field(default_factory=list[str])
    person_tokens: list[str] = field(default_factory=list[str])
    exits: int = 0
    hold: bool = False
    unavailable: BaseException | None = None
    deltas: list[str] = field(default_factory=lambda: ["Eine Antwort."])
    busy_events: int | None = None
    event_hook: Callable[[int], None] | None = None
    generated_events: int = 0
    reader_closed: bool = False
    reader_closed_before_context_exit: bool = False

    def answer(
        self, *, message: str, person_token: TokenSource
    ) -> AbstractAsyncContextManager[AsyncIterator[str]]:
        @asynccontextmanager
        async def operation() -> AsyncGenerator[AsyncIterator[str]]:
            self.messages.append(message)
            self.person_tokens.append(await person_token())
            if self.unavailable is not None:
                raise self.unavailable

            async def texts() -> AsyncIterator[str]:
                try:
                    if self.busy_events is not None:
                        for index in range(self.busy_events):
                            if self.event_hook is not None:
                                self.event_hook(index)
                            self.generated_events += 1
                            yield f"Antwort {index} "
                            await asyncio.sleep(0)
                        return
                    for delta in self.deltas:
                        yield delta
                        if self.hold:
                            await asyncio.Future()
                finally:
                    self.reader_closed = True

            try:
                yield texts()
            finally:
                self.reader_closed_before_context_exit = self.reader_closed
                self.exits += 1

        return operation()


@dataclass
class FakeSpeaking:
    """Speaks every sentence as the same WAV header, and remembers what it spoke."""

    spoken: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fails_at: int | None = None

    async def speak(self, text: str, language: str) -> bytes:
        if self.fails_at == len(self.spoken):
            raise CoPresenterUnavailableError
        self.spoken.append((text, language))
        return b"RIFF"


@dataclass
class FakePersonAccess:
    """Gives every session the same person token, or refuses it."""

    refusal: AccessRefusal | None = None
    unavailable: bool = False
    sessions: list[str] = field(default_factory=list[str])

    async def fresh(self, session_id: str) -> PersonToken | AccessRefusal:
        self.sessions.append(session_id)
        if self.unavailable:
            raise CoPresenterUnavailableError
        return self.refusal or PersonToken("person-token")


@dataclass
class FakeDeckTexts:
    """Reads the same words for every deck, and remembers which it was asked for."""

    text: DeckText | None = field(default_factory=lambda: A_DECK_TEXT)
    unreadable: bool = False
    asked: list[tuple[Deck, Source]] = field(default_factory=list[tuple[Deck, Source]])

    def text_of(self, deck: Deck, source: Source) -> DeckText | None:
        self.asked.append((deck, source))
        if self.unreadable:
            raise CoPresenterUnavailableError
        return self.text


@dataclass(kw_only=True)
class PrivateFakes:
    """One fake for each port the use case reaches through."""

    answers: RecordingAnswers = field(default_factory=RecordingAnswers)
    speech: RecordingSpeech = field(default_factory=RecordingSpeech)
    speaking: FakeSpeaking = field(default_factory=FakeSpeaking)
    person_access: FakePersonAccess = field(default_factory=FakePersonAccess)
    texts: FakeDeckTexts = field(default_factory=FakeDeckTexts)


def a_use(fakes: PrivateFakes, decks: FakeDeckStore) -> CoPresenterUse:
    sources = FakeSourceStore(sources=[A_SOURCE])
    return CoPresenterUse(
        answers=fakes.answers,
        speech=fakes.speech,
        decks=decks,
        sources=sources,
        texts=fakes.texts,
        boundaries=split_sentences,
        speaking=fakes.speaking,
        person_access=fakes.person_access,
    )


def a_copresenter_talk_host(
    *,
    users: FakeUserStore | None = None,
    delay: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: AskLimiter | None = None,
    decks: FakeDeckStore | None = None,
) -> tuple[Lobby, PrivateFakes, FrozenClock]:
    private = PrivateFakes()
    people = a_crowd(users)
    app = a_talk_app(
        people=people,
        copresenter=CoPresenterSurface(
            use=a_use(private, decks or the_admins_decks()),
            limiter=limiter or AskLimiter(clock=FrozenClock(instant=NOW)),
            browser_origin=TALK_ORIGIN,
            delay=delay,
        ),
    )
    browser = a_browser_at(
        app,
        people=people,
        base_url=A_TALK_ADDRESS,
        headers=A_BROWSERS_HEADERS,
    )
    return browser, private, people.clock


def ask(client: TestClient, body: Mapping[str, object] = QUESTION) -> Response:
    return client.post(
        "/copresenter/ask",
        json=body,
        headers={"x-csrf-token": client.cookies[DEFAULT_CSRF_COOKIE_NAME]},
    )


@dataclass
class ManualDeadline:
    """A deadline released from the test without sleeping or resetting it."""

    started: threading.Event = field(default_factory=threading.Event)
    _checks: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = field(
        default_factory=list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]]
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def wait(self, _seconds: float) -> None:
        loop = asyncio.get_running_loop()
        check = loop.create_future()
        with self._lock:
            self._checks.append((loop, check))
            self.started.set()
        await asyncio.shield(check)

    def release_first(self) -> None:
        with self._lock:
            loop, check = self._checks[0]
        loop.call_soon_threadsafe(_complete, check)


def _complete(check: asyncio.Future[None]) -> None:
    if not check.done():
        check.set_result(None)
