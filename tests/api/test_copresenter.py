"""The browser co-presenter path uses the existing login and narrow inputs."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from presentator.api.copresenter import CoPresenterSurface
from presentator.application.copresenter import CoPresenterUse
from presentator.application.identity import IDLE_WINDOW
from presentator.contracts.copresenter import (
    AnswerEvent,
    Audio,
    CoPresenterReadiness,
    Done,
    HearingTranscript,
    HearingUnavailable,
    Question,
    Text,
)
from presentator.contracts.models import Account, Role, User
from tests.api.lobby import A_BROWSERS_HEADERS, TYPED_WORDS, USERNAME, a_lobby_app
from tests.application.fakes import FakeUserStore, FrozenClock, ReversibleHasher

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from presentator.ports.copresenter import PrivateHearing

PUBLIC_ORIGIN = "https://presentator.test"
QUESTION = {"said": "Was ist wichtig?", "slide": 2, "language": "de"}
POLICY_VIOLATION = 1008
UNSUPPORTED_DATA = 1003


@dataclass
class RecordingHearing:
    """The private hearing child whose use the route makes observable."""

    frames: list[bytes] = field(default_factory=list[bytes])
    closed: int = 0
    events: list[HearingTranscript | HearingUnavailable] = field(
        default_factory=lambda: [HearingTranscript(text="gehört", final=True)]
    )

    async def send_pcm(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def receive(self) -> HearingTranscript | HearingUnavailable | None:
        if self.events:
            return self.events.pop(0)
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

    async def readiness(self) -> CoPresenterReadiness:
        self.readiness_calls += 1
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

            async def events() -> AsyncIterator[AnswerEvent]:
                yield Text(text="Eine Antwort")
                if self.hold_answers:
                    await asyncio.Future()
                yield Audio(text="Eine Antwort", wav=b"RIFF")
                yield Done(text="Eine Antwort")

            try:
                yield events()
            finally:
                self.answer_exits += 1

        return operation()

    def hear(self, language: str) -> AbstractAsyncContextManager[PrivateHearing]:
        @asynccontextmanager
        async def operation() -> AsyncGenerator[PrivateHearing]:
            assert language == "de"
            self.hearing_opens += 1
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


def test_session_store_loss_cancels_an_accepted_answer_on_the_one_second_check() -> (
    None
):
    users = RefusingUserStore()
    checked_intervals: list[float] = []

    async def lose_store(interval: float) -> None:
        checked_intervals.append(interval)
        users.fail_reads = True
        await asyncio.sleep(0)

    client, private, _clock = a_copresenter_lobby(users=users, delay=lose_store)
    private.hold_answers = True
    sign_in(client)

    response = client.post(
        "/copresenter/ask",
        json=QUESTION,
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.content == b""
    assert private.questions == [
        Question(said="Was ist wichtig?", slide=2, language="de")
    ]
    assert private.answer_exits == 1
    assert checked_intervals == [1.0]


def test_deactivation_closes_an_admitted_hearing_before_private_use() -> None:
    users = RefusingUserStore()
    checked_intervals: list[float] = []

    async def deactivate(interval: float) -> None:
        checked_intervals.append(interval)
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
    assert checked_intervals == [1.0]
