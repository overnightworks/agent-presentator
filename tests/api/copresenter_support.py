"""The co-presenter talk host's shared fakes, values and builders."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME

from presentator.api.copresenter import (
    CoPresenterSurface,
)
from presentator.application.copresenter import AskLimiter, CoPresenterUse
from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerText,
    AskedDeck,
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    Done,
    HearingTranscript,
    Question,
)
from presentator.contracts.decks import Source
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

    from presentator.ports.copresenter import PrivateHearing

TALK_ORIGIN = A_TALK_ADDRESS
A_DECK = "kundenfeedback"
QUESTION = {"said": "Was ist wichtig?", "slide": 2, "language": "de", "deck": A_DECK}
A_SOURCE = Source(id=SOURCE_ID, name="s", url="u", ref="main", owner_id=ADMIN)


def the_admins_decks() -> FakeDeckStore:
    return a_deck_store(slug=A_DECK, built=a_build())


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
class RecordingPrivateCoPresenter:
    """A recording private service behind the real application use case."""

    readiness_calls: int = 0
    questions: list[Question] = field(default_factory=list[Question])
    answer_exits: int = 0
    hold_answers: bool = False
    hearing_opens: int = 0
    hearing: RecordingHearing = field(default_factory=RecordingHearing)
    readiness_unavailable: bool = False
    answer_unavailable: bool = False
    hearing_unavailable: bool = False
    grouped_hearing_context: bool = False
    busy_answer_events: int | None = None
    answer_events: list[AnswerEvent] | None = None
    answer_event_hook: Callable[[int], None] | None = None
    generated_answer_events: int = 0
    answer_reader_closed: bool = False
    answer_reader_closed_before_context_exit: bool = False

    async def readiness(self) -> CoPresenterReadiness:
        self.readiness_calls += 1
        if self.readiness_unavailable:
            raise CoPresenterUnavailableError
        return CoPresenterReadiness(
            hearing_sample_rate=16_000,
            local_hearing_ready=True,
        )

    def answer(
        self,
        question: Question,
    ) -> AbstractAsyncContextManager[AsyncIterator[AnswerEvent]]:
        @asynccontextmanager
        async def operation() -> AsyncGenerator[AsyncIterator[AnswerEvent]]:
            self.questions.append(question)
            if self.answer_unavailable:
                raise CoPresenterUnavailableError

            async def events() -> AsyncIterator[AnswerEvent]:
                try:
                    if self.answer_events is not None:
                        for event in self.answer_events:
                            yield event
                        return
                    if self.busy_answer_events is not None:
                        for index in range(self.busy_answer_events):
                            if self.answer_event_hook is not None:
                                self.answer_event_hook(index)
                            self.generated_answer_events += 1
                            yield AnswerText(text=f"Antwort {index}")
                            await asyncio.sleep(0)
                        return
                    yield AnswerText(text="Eine Antwort")
                    if self.hold_answers:
                        await asyncio.Future()
                    yield Audio(text="Eine Antwort", wav=b"RIFF")
                    yield Done(text="Eine Antwort")
                finally:
                    self.answer_reader_closed = True

            try:
                yield events()
            finally:
                self.answer_reader_closed_before_context_exit = (
                    self.answer_reader_closed
                )
                self.answer_exits += 1

        return operation()

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


def a_use(private: RecordingPrivateCoPresenter, decks: FakeDeckStore) -> CoPresenterUse:
    sources = FakeSourceStore(sources=[A_SOURCE])
    return CoPresenterUse(answers=private, speech=private, decks=decks, sources=sources)


def a_copresenter_talk_host(
    *,
    users: FakeUserStore | None = None,
    delay: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: AskLimiter | None = None,
    decks: FakeDeckStore | None = None,
) -> tuple[Lobby, RecordingPrivateCoPresenter, FrozenClock]:
    private = RecordingPrivateCoPresenter()
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
