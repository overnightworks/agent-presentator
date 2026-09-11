"""The browser co-presenter path uses the existing login and narrow inputs."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from presentator.api.copresenter import CoPresenterSurface, add_copresenter_routes
from presentator.application.copresenter import CoPresenterUse
from presentator.application.identity import IDLE_WINDOW
from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerUnavailable,
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    Done,
    HearingTranscript,
    HearingUnavailable,
    Question,
    Sentence,
    Text,
)
from presentator.contracts.models import Account, Role, User
from tests.api.lobby import A_BROWSERS_HEADERS, TYPED_WORDS, USERNAME, a_lobby_app
from tests.application.fakes import FakeUserStore, FrozenClock, ReversibleHasher

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from starlette.requests import HTTPConnection

    from presentator.ports.copresenter import PrivateHearing

PUBLIC_ORIGIN = "https://presentator.test"
QUESTION = {"said": "Was ist wichtig?", "slide": 2, "language": "de"}
POLICY_VIOLATION = 1008
UNSUPPORTED_DATA = 1003
INTERNAL_ERROR = 1011


@dataclass
class RecordingHearing:
    """The private hearing child whose use the route makes observable."""

    frames: list[bytes] = field(default_factory=list[bytes])
    closed: int = 0
    events: list[HearingTranscript | HearingUnavailable | RuntimeError] = field(
        default_factory=lambda: [HearingTranscript(text="gehört", final=True)]
    )
    _frame_ready: asyncio.Queue[None] | None = field(default=None, init=False)

    async def send_pcm(self, frame: bytes) -> None:
        self.frames.append(frame)
        if self._frame_ready is None:
            self._frame_ready = asyncio.Queue()
        self._frame_ready.put_nowait(None)

    async def receive(self) -> HearingTranscript | HearingUnavailable | None:
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
                            yield Text(text=f"Antwort {index}")
                            await asyncio.sleep(0)
                        return
                    yield Text(text="Eine Antwort")
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
            yield self.hearing

        return operation()


def a_copresenter_lobby(
    *,
    users: FakeUserStore | None = None,
    delay: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[TestClient, RecordingPrivateCoPresenter, FrozenClock]:
    private = RecordingPrivateCoPresenter()
    app, clock = a_lobby_app(
        users=users,
        copresenter=CoPresenterSurface(
            use=CoPresenterUse(private=private),
            public_origin=PUBLIC_ORIGIN,
            delay=delay,
        ),
    )
    return (
        TestClient(app, follow_redirects=False, headers=A_BROWSERS_HEADERS),
        private,
        clock,
    )


def sign_in(client: TestClient) -> None:
    client.post(
        "/setup",
        data={
            "username": USERNAME,
            "password": TYPED_WORDS,
            "repeated_password": TYPED_WORDS,
        },
    )


def test_signed_out_and_forged_sessions_never_reach_the_private_service() -> None:
    client, private, _clock = a_copresenter_lobby()

    signed_out = client.get("/copresenter/who", headers={"accept": "application/json"})
    client.cookies.set("presentator_session", "forged")
    forged = client.get("/copresenter/who", headers={"accept": "application/json"})

    assert signed_out.status_code == HTTPStatus.UNAUTHORIZED
    assert forged.status_code == HTTPStatus.UNAUTHORIZED
    assert private.readiness_calls == 0

    for cookie in (None, "forged"):
        if cookie is None:
            client.cookies.clear()
        else:
            client.cookies.set("presentator_session", cookie)
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(
                "/copresenter/hear?language=de",
                headers={"origin": PUBLIC_ORIGIN},
            ),
        ):
            pass
        assert refused.value.code == POLICY_VIOLATION
    assert private.hearing_opens == 0


def test_copresenter_routes_refuse_unauthenticated_http_before_private_use() -> None:
    private = RecordingPrivateCoPresenter()
    app = FastAPI()

    def no_session(_connection: HTTPConnection) -> None:
        return None

    add_copresenter_routes(
        app,
        surface=CoPresenterSurface(
            use=CoPresenterUse(private=private),
            public_origin=PUBLIC_ORIGIN,
        ),
        admission=no_session,
    )
    client = TestClient(app)

    who = client.get("/copresenter/who")
    answer = client.post("/copresenter/ask", json=QUESTION)

    assert who.status_code == HTTPStatus.UNAUTHORIZED
    assert answer.status_code == HTTPStatus.UNAUTHORIZED
    assert private.readiness_calls == 0
    assert private.questions == []


@pytest.mark.parametrize("role", [Role.ADMIN, Role.USER])
def test_both_roles_receive_only_the_three_readiness_values(role: Role) -> None:
    if role is Role.ADMIN:
        client, private, _clock = a_copresenter_lobby()
        sign_in(client)
    else:
        account = Account(
            id="the-user",
            username=USERNAME,
            role=role,
            password_hash=ReversibleHasher().hash(TYPED_WORDS),
        )
        client, private, _clock = a_copresenter_lobby(
            users=FakeUserStore(accounts={USERNAME: account}),
        )
        client.post(
            "/login",
            data={"username": USERNAME, "password": TYPED_WORDS},
        )

    response = client.get("/copresenter/who")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "answerer": {"model": "canned"},
        "speech": {"sample_rate": 16_000, "hearing": {"ready": True}},
    }
    assert private.readiness_calls == 1


def test_ask_requires_the_session_csrf_token_and_streams_only_public_events() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

    refused = client.post("/copresenter/ask", json=QUESTION)
    response = client.post(
        "/copresenter/ask",
        json=QUESTION,
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert response.status_code == HTTPStatus.OK
    assert "event: text" in response.text
    assert "event: audio" in response.text
    assert "UklGRg==" in response.text
    assert private.questions == [
        Question(said="Was ist wichtig?", slide=2, language="de")
    ]


def test_private_answer_events_preserve_the_public_stream_contract() -> None:
    client, private, _clock = a_copresenter_lobby()
    private.answer_events = [
        Text(text="Der Anfang"),
        Sentence(text="Der Satz."),
        Done(text="Die Antwort."),
    ]
    sign_in(client)

    response = client.post(
        "/copresenter/ask",
        json=QUESTION,
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.text == (
        'event: text\ndata: {"text": "Der Anfang"}\n\n'
        'event: sentence\ndata: {"text": "Der Satz."}\n\n'
        'event: done\ndata: {"text": "Die Antwort."}\n\n'
    )
    assert private.answer_exits == 1


def test_private_answer_refusal_and_clean_end_do_not_invent_an_answer() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)
    csrf = {"x-csrf-token": client.cookies["csrf_token"]}

    private.answer_events = [AnswerUnavailable()]
    refused = client.post("/copresenter/ask", json=QUESTION, headers=csrf)
    private.answer_events = []
    ended = client.post("/copresenter/ask", json=QUESTION, headers=csrf)

    assert refused.text == (
        'event: error\ndata: {"message": "the answer could not be spoken"}\n\n'
    )
    assert ended.text == ""
    assert private.answer_exits == len((refused, ended))


def test_foreign_origin_and_text_frames_open_no_private_hearing() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

    with (
        pytest.raises(WebSocketDisconnect) as foreign,
        client.websocket_connect(
            "/copresenter/hear?language=de",
            headers={"origin": "https://foreign.test"},
        ),
    ):
        pass
    assert foreign.value.code == POLICY_VIOLATION

    with (
        pytest.raises(WebSocketDisconnect) as extra_query,
        client.websocket_connect(
            "/copresenter/hear?language=de&credential=must-not-travel",
            headers={"origin": PUBLIC_ORIGIN},
        ),
    ):
        pass
    assert extra_query.value.code == POLICY_VIOLATION

    with (
        client.websocket_connect(
            "/copresenter/hear?language=de",
            headers={"origin": PUBLIC_ORIGIN},
        ) as socket,
    ):
        socket.send_text("not pcm")
        with pytest.raises(WebSocketDisconnect) as wrong_type:
            socket.receive_json()

    assert wrong_type.value.code == UNSUPPORTED_DATA
    assert private.hearing_opens == 0


def test_browser_disconnect_before_pcm_opens_no_private_hearing() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ):
        pass

    assert private.hearing_opens == 0


def test_an_active_session_keeps_hearing_after_an_authoritative_recheck() -> None:
    deadline = ManualDeadline()
    client, private, _clock = a_copresenter_lobby(delay=deadline.wait)
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        deadline.release_first()
        socket.send_bytes(b"pcm after the session check")
        assert socket.receive_json() == {"text": "gehört", "final": True}

    assert private.hearing.frames == [b"pcm after the session check"]
    assert private.hearing.closed == 1


def test_binary_hearing_forwards_pcm_and_returns_only_typed_transcripts() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        socket.send_bytes(b"pcm")
        assert socket.receive_json() == {"text": "gehört", "final": True}

    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1


def test_an_accepted_hearing_refuses_text_after_private_use_started() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        socket.send_bytes(b"pcm")
        assert socket.receive_json() == {"text": "gehört", "final": True}
        socket.send_text("browser data is never private PCM")
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()

    assert refused.value.code == UNSUPPORTED_DATA
    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1


def test_private_hearing_failure_keeps_the_authenticated_browser_lease() -> None:
    client, private, _clock = a_copresenter_lobby()
    private.hearing.events = [HearingUnavailable()]
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        socket.send_bytes(b"first")
        assert socket.receive_json() == {
            "text": "",
            "final": True,
            "error": "hearing unavailable",
        }
        socket.send_bytes(b"browser recognition keeps the lease alive")

    assert private.hearing.frames == [b"first"]
    assert private.hearing.closed == 1


def test_browser_fallback_refuses_text_frames_without_reopening_private_hearing() -> (
    None
):
    client, private, _clock = a_copresenter_lobby()
    private.hearing.events = [HearingUnavailable()]
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        socket.send_bytes(b"local hearing fails")
        assert socket.receive_json()["error"] == "hearing unavailable"
        socket.send_text("browser fallback remains binary-only")
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()

    assert refused.value.code == UNSUPPORTED_DATA
    assert private.hearing.frames == [b"local hearing fails"]
    assert private.hearing.closed == 1


def test_unexpected_private_hearing_failure_closes_the_public_connection() -> None:
    client, private, _clock = a_copresenter_lobby()
    private.hearing.events = [RuntimeError("private hearing failed")]
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        socket.send_bytes(b"pcm")
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()

    assert refused.value.code == INTERNAL_ERROR
    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1


def test_private_failures_expose_only_public_failure_shapes() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)
    private.readiness_unavailable = True

    who = client.get("/copresenter/who")
    private.readiness_unavailable = False
    private.answer_unavailable = True
    answer = client.post(
        "/copresenter/ask",
        json=QUESTION,
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    private.hearing_unavailable = True
    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        socket.send_bytes(b"pcm")
        hearing = socket.receive_json()

    assert who.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert who.json() == {"detail": "Co-presenter unavailable"}
    assert answer.text == (
        'event: error\ndata: {"message": "the answer could not be spoken"}\n\n'
    )
    assert hearing == {"text": "", "final": True, "error": "hearing unavailable"}


def test_local_allowlist_refuses_extra_fields_methods_and_paths() -> None:
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)
    csrf = {"x-csrf-token": client.cookies["csrf_token"]}

    unknown_field = client.post(
        "/copresenter/ask",
        json={**QUESTION, "cookie": "must not travel"},
        headers=csrf,
    )
    wrong_method = client.put("/copresenter/who")
    unknown_path = client.get("/copresenter/private")

    assert unknown_field.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert wrong_method.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert unknown_path.status_code == HTTPStatus.NOT_FOUND
    assert private.questions == []


def test_expired_and_logged_out_sessions_are_refused_before_private_use() -> None:
    expired, expired_private, clock = a_copresenter_lobby()
    sign_in(expired)
    clock.advance(IDLE_WINDOW + timedelta(seconds=1))

    assert expired.get("/copresenter/who").status_code == HTTPStatus.FOUND
    assert expired_private.readiness_calls == 0

    logged_out, logged_out_private, _clock = a_copresenter_lobby()
    sign_in(logged_out)
    old_cookie = logged_out.cookies["presentator_session"]
    logged_out.post("/logout")
    logged_out.cookies.set("presentator_session", old_cookie)

    assert logged_out.get("/copresenter/who").status_code == HTTPStatus.FOUND
    assert logged_out_private.readiness_calls == 0


class RefusingUserStore(FakeUserStore):
    """A user store that can deactivate a person or fail its read boundary."""

    refuse_reads: bool = False
    fail_reads: bool = False

    def get(self, user_id: str) -> User | None:
        if self.fail_reads:
            message = "session store unavailable"
            raise RuntimeError(message)
        found = super().get(user_id)
        return None if self.refuse_reads else found


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


@pytest.mark.parametrize("failure", ["deactivated", "store-error"])
def test_identity_loss_fails_closed_before_private_use(failure: str) -> None:
    account = Account(
        id="the-user",
        username=USERNAME,
        role=Role.USER,
        password_hash=ReversibleHasher().hash(TYPED_WORDS),
    )
    users = RefusingUserStore(accounts={USERNAME: account})
    client, private, _clock = a_copresenter_lobby(users=users)
    client.post("/login", data={"username": USERNAME, "password": TYPED_WORDS})

    users.refuse_reads = failure == "deactivated"
    users.fail_reads = failure == "store-error"
    if users.fail_reads:
        with pytest.raises(RuntimeError, match="session store unavailable"):
            client.get("/copresenter/who")
    else:
        assert client.get("/copresenter/who").status_code == HTTPStatus.FOUND

    assert private.readiness_calls == 0


def test_session_store_failure_refuses_websocket_admission_before_private_use() -> None:
    users = RefusingUserStore()
    client, private, _clock = a_copresenter_lobby(users=users)
    sign_in(client)
    users.fail_reads = True

    with (
        pytest.raises(RuntimeError, match="session store unavailable"),
        client.websocket_connect(
            "/copresenter/hear?language=de",
            headers={"origin": PUBLIC_ORIGIN},
        ),
    ):
        pass

    assert private.hearing_opens == 0


def test_session_store_loss_cancels_an_accepted_answer_at_its_deadline() -> None:
    users = RefusingUserStore()

    async def lose_store(_seconds: float) -> None:
        for _turn in range(5):
            await asyncio.sleep(0)
        users.fail_reads = True

    client, private, _clock = a_copresenter_lobby(users=users, delay=lose_store)
    private.hold_answers = True
    sign_in(client)

    response = client.post(
        "/copresenter/ask",
        json=QUESTION,
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == HTTPStatus.OK
    assert "event: text" in response.text
    assert "event: audio" not in response.text
    assert "event: done" not in response.text
    assert private.questions == [
        Question(said="Was ist wichtig?", slide=2, language="de")
    ]
    assert private.answer_exits == 1
    assert private.answer_reader_closed_before_context_exit


def test_deactivation_closes_an_admitted_hearing_before_private_use() -> None:
    users = RefusingUserStore()

    async def deactivate(_seconds: float) -> None:
        users.refuse_reads = True
        await asyncio.sleep(0)

    client, private, _clock = a_copresenter_lobby(users=users, delay=deactivate)
    sign_in(client)

    with (
        client.websocket_connect(
            "/copresenter/hear?language=de",
            headers={"origin": PUBLIC_ORIGIN},
        ) as socket,
        pytest.raises(WebSocketDisconnect) as ended,
    ):
        socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing_opens == 0


def test_busy_answer_stops_at_its_persistent_authorization_deadline() -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    event_count = 40
    refuse_after = 2

    def end_session(index: int) -> None:
        if index == refuse_after:
            users.refuse_reads = True
            deadline.release_first()

    client, private, _clock = a_copresenter_lobby(users=users, delay=deadline.wait)
    private.busy_answer_events = event_count
    private.answer_event_hook = end_session
    sign_in(client)

    response = client.post(
        "/copresenter/ask",
        json=QUESTION,
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.text.count("event: text") < event_count
    assert private.generated_answer_events < event_count
    assert private.answer_exits == 1


@pytest.mark.parametrize("loss", ["deactivated", "store-error"])
def test_busy_local_hearing_stops_before_more_pcm_after_authoritative_loss(
    loss: str,
) -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    client, private, _clock = a_copresenter_lobby(users=users, delay=deadline.wait)
    private.hearing.events = [
        HearingTranscript(text="eins", final=True),
        HearingTranscript(text="zwei", final=True),
    ]
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        socket.send_bytes(b"before loss")
        assert socket.receive_json()["text"] == "eins"
        frames_before_loss = list(private.hearing.frames)
        users.refuse_reads = loss == "deactivated"
        users.fail_reads = loss == "store-error"
        deadline.release_first()
        socket.send_bytes(b"after loss")
        socket.send_text("force an observable close if the deadline was reset")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing.frames == frames_before_loss
    assert private.hearing.closed == 1


@pytest.mark.parametrize("loss", ["deactivated", "store-error"])
def test_busy_browser_fallback_keeps_the_original_authorization_deadline(
    loss: str,
) -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    client, private, _clock = a_copresenter_lobby(users=users, delay=deadline.wait)
    private.hearing.events = [HearingUnavailable()]
    sign_in(client)

    with client.websocket_connect(
        "/copresenter/hear?language=de",
        headers={"origin": PUBLIC_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        socket.send_bytes(b"local hearing fails")
        assert socket.receive_json()["error"] == "hearing unavailable"
        users.refuse_reads = loss == "deactivated"
        users.fail_reads = loss == "store-error"
        deadline.release_first()
        socket.send_bytes(b"busy fallback lease")
        socket.send_text("force an observable close if the deadline was reset")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing.frames == [b"local hearing fails"]
