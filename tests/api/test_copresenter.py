"""The browser co-presenter path uses the existing login and narrow inputs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME

from presentator.api.copresenter import (
    CoPresenterSurface,
    add_copresenter_routes,
)
from presentator.application.copresenter import AskLimiter, AskLimits
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.copresenter import (
    AnswerText,
    AnswerUnavailable,
    Done,
    HearingTranscript,
    Question,
    Sentence,
)
from presentator.contracts.models import Role, User
from tests.api.copresenter_support import (
    ASKED,
    HEARING,
    INTERNAL_ERROR,
    POLICY_VIOLATION,
    QUESTION,
    TALK_ORIGIN,
    UNSUPPORTED_DATA,
    ManualDeadline,
    RecordingPrivateCoPresenter,
    a_copresenter_talk_host,
    a_use,
    ask,
)
from tests.api.lobby import (
    NOW,
)
from tests.application.fakes import (
    FakeDeckStore,
    FakeUserStore,
    FrozenClock,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection
    from starlette.types import Message, Scope


def test_signed_out_and_forged_sessions_never_reach_the_private_service() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client

    signed_out = client.get("/copresenter/who", headers={"accept": "application/json"})
    client.cookies.set(DEFAULT_SESSION_COOKIE_NAME, "forged")
    forged = client.get("/copresenter/who", headers={"accept": "application/json"})

    assert signed_out.status_code == HTTPStatus.UNAUTHORIZED
    assert forged.status_code == HTTPStatus.UNAUTHORIZED
    assert private.readiness_calls == 0

    for cookie in (None, "forged"):
        if cookie is None:
            client.cookies.clear()
        else:
            client.cookies.set(DEFAULT_SESSION_COOKIE_NAME, cookie)
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(
                HEARING,
                headers={"origin": TALK_ORIGIN},
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
            use=a_use(private, FakeDeckStore()),
            limiter=AskLimiter(clock=FrozenClock(instant=NOW)),
            browser_origin=TALK_ORIGIN,
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


def test_the_admin_receives_only_speech_readiness() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in(role=Role.ADMIN)

    response = client.get("/copresenter/who")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "speech": {"sample_rate": 16_000, "hearing": {"ready": True}},
    }
    assert private.readiness_calls == 1


def test_a_signed_in_non_admin_is_refused_every_copresenter_route() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in(role=Role.USER)

    who = client.get("/copresenter/who")
    answer = ask(client)
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(HEARING, headers={"origin": TALK_ORIGIN}),
    ):
        pass

    assert who.status_code == HTTPStatus.FORBIDDEN
    assert answer.status_code == HTTPStatus.FORBIDDEN
    assert refused.value.code == POLICY_VIOLATION
    assert private.readiness_calls == 0
    assert private.questions == []
    assert private.hearing_opens == 0


def test_a_running_hearing_ends_when_its_session_loses_the_admin_role() -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=deadline.wait)
    lobby.sign_in(role=Role.ADMIN)

    with lobby.client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        _demote(users)
        deadline.release_first()
        socket.send_text("force an observable close if the hearing stood")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing_opens == 0


def _demote(users: FakeUserStore) -> None:
    (admin,) = users.users.values()
    users.users[admin.id] = replace(admin, role=Role.USER)


def test_a_running_hearing_with_audio_ends_when_its_session_loses_the_admin_role() -> (
    None
):
    users = RefusingUserStore()
    deadline = ManualDeadline()
    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=deadline.wait)
    lobby.sign_in(role=Role.ADMIN)

    with lobby.client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        socket.send_bytes(b"before demotion")
        assert socket.receive_json() == {"text": "gehört", "final": True}
        _demote(users)
        deadline.release_first()
        socket.send_text("force an observable close if the hearing stood")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing.frames == [b"before demotion"]
    assert private.hearing.closed == 1


def test_an_answer_in_flight_stops_when_its_session_loses_the_admin_role() -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    event_count = 40
    demote_after = 2

    def demote_mid_answer(index: int) -> None:
        if index == demote_after:
            _demote(users)
            deadline.release_first()

    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=deadline.wait)
    private.busy_answer_events = event_count
    private.answer_event_hook = demote_mid_answer
    lobby.sign_in(role=Role.ADMIN)
    client = lobby.client

    response = ask(client)

    assert response.status_code == HTTPStatus.OK
    assert response.text.count("event: text") < event_count
    assert private.generated_answer_events < event_count
    assert private.answer_exits == 1


def test_a_refused_non_admin_question_keeps_the_sessions_ask_capacity() -> None:
    users = RefusingUserStore()
    limiter = AskLimiter(clock=FrozenClock(instant=NOW), limits=AskLimits(per_window=1))
    lobby, private, _clock = a_copresenter_talk_host(users=users, limiter=limiter)
    lobby.sign_in(role=Role.USER)

    refused = ask(lobby.client)
    (person,) = users.users.values()
    users.users[person.id] = replace(person, role=Role.ADMIN)
    answered = ask(lobby.client)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert answered.status_code == HTTPStatus.OK
    assert len(private.questions) == 1


def test_ask_requires_the_session_csrf_token_and_streams_only_public_events() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    refused = client.post("/copresenter/ask", json=QUESTION)
    response = ask(client)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert response.status_code == HTTPStatus.OK
    assert "event: text" in response.text
    assert "event: audio" in response.text
    assert "UklGRg==" in response.text
    assert private.questions == [
        Question(said="Was ist wichtig?", slide=2, language="de", deck=ASKED)
    ]


def test_private_answer_events_preserve_the_public_stream_contract() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.answer_events = [
        AnswerText(text="Der Anfang"),
        Sentence(text="Der Satz."),
        Done(text="Die Antwort."),
    ]
    lobby.sign_in()

    response = ask(client)

    assert response.text == (
        'event: text\ndata: {"text": "Der Anfang"}\n\n'
        'event: sentence\ndata: {"text": "Der Satz."}\n\n'
        'event: done\ndata: {"text": "Die Antwort."}\n\n'
    )
    assert private.answer_exits == 1


def test_private_answer_refusal_and_clean_end_do_not_invent_an_answer() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    private.answer_events = [AnswerUnavailable()]
    refused = ask(client)
    private.answer_events = []
    ended = ask(client)

    assert refused.text == (
        'event: error\ndata: {"message": "the answer could not be spoken"}\n\n'
    )
    assert ended.text == ""
    assert private.answer_exits == len((refused, ended))


def test_foreign_origin_and_text_frames_open_no_private_hearing() -> None:
    lobby, private, clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()
    clock.advance(timedelta(minutes=5))

    for headers in ({"origin": "https://foreign.test"}, {}):
        with (
            pytest.raises(WebSocketDisconnect) as foreign,
            client.websocket_connect(HEARING, headers=headers),
        ):
            pass
        assert foreign.value.code == POLICY_VIOLATION
    assert lobby.people.sessions.loads == 0

    with (
        pytest.raises(WebSocketDisconnect) as extra_query,
        client.websocket_connect(
            f"{HEARING}&credential=must-not-travel",
            headers={"origin": TALK_ORIGIN},
        ),
    ):
        pass
    assert extra_query.value.code == POLICY_VIOLATION

    with (
        client.websocket_connect(
            HEARING,
            headers={"origin": TALK_ORIGIN},
        ) as socket,
    ):
        socket.send_text("not pcm")
        with pytest.raises(WebSocketDisconnect) as wrong_type:
            socket.receive_json()

    assert wrong_type.value.code == UNSUPPORTED_DATA
    assert private.hearing_opens == 0


def test_browser_disconnect_before_pcm_opens_no_private_hearing() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ):
        pass

    assert private.hearing_opens == 0


def test_an_active_session_keeps_hearing_after_an_authoritative_recheck() -> None:
    deadline = ManualDeadline()
    lobby, private, _clock = a_copresenter_talk_host(delay=deadline.wait)
    client = lobby.client
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        deadline.release_first()
        socket.send_bytes(b"pcm after the session check")
        assert socket.receive_json() == {"text": "gehört", "final": True}

    assert private.hearing.frames == [b"pcm after the session check"]
    assert private.hearing.closed == 1


def test_binary_hearing_forwards_pcm_and_returns_only_typed_transcripts() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.hearing.events = [
        HearingTranscript(text="erste Antwort", final=False),
        HearingTranscript(text="zweite Antwort", final=True),
        HearingTranscript(text="leere Antwort", final=False),
    ]
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        socket.send_bytes(b"erstes pcm")
        assert socket.receive_json() == {"text": "erste Antwort", "final": False}
        socket.send_bytes(b"zweites pcm")
        assert socket.receive_json() == {"text": "zweite Antwort", "final": True}
        socket.send_bytes(b"")
        assert socket.receive_json() == {"text": "leere Antwort", "final": False}

    assert private.hearing.frames == [b"erstes pcm", b"zweites pcm", b""]
    assert private.hearing.closed == 1


def test_transcript_delivery_disconnect_closes_the_private_child() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.grouped_hearing_context = True
    lobby.sign_in()

    asyncio.run(_send_hearing_event_to_a_disconnected_browser(client))

    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1


def test_private_failure_notice_disconnect_closes_the_private_child() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.hearing.events = [None]
    lobby.sign_in()

    asyncio.run(_send_hearing_event_to_a_disconnected_browser(client))

    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1


async def _send_hearing_event_to_a_disconnected_browser(client: TestClient) -> None:
    messages: list[Message] = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "bytes": b"pcm"},
    ]
    browser_send_attempted = False

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        return await asyncio.Future()

    async def send(message: Message) -> None:
        nonlocal browser_send_attempted
        if message["type"] == "websocket.send":
            browser_send_attempted = True
            raise OSError

    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "scheme": "wss",
        "path": "/copresenter/hear",
        "raw_path": b"/copresenter/hear",
        "query_string": b"language=de",
        "headers": [
            (b"host", b"testserver"),
            (b"origin", TALK_ORIGIN.encode()),
            (
                b"cookie",
                f"{DEFAULT_SESSION_COOKIE_NAME}={client.cookies[DEFAULT_SESSION_COOKIE_NAME]}".encode(),
            ),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "subprotocols": [],
        "state": {},
    }

    await client.app(scope, receive, send)

    assert browser_send_attempted


def test_an_accepted_hearing_refuses_text_after_private_use_started() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
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
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.hearing.events = [None]
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
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
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.hearing.events = [None]
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
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
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    private.hearing.events = [RuntimeError("private hearing failed")]
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        socket.send_bytes(b"pcm")
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()

    assert refused.value.code == INTERNAL_ERROR
    assert private.hearing.frames == [b"pcm"]
    assert private.hearing.closed == 1


def test_private_failures_expose_only_public_failure_shapes() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()
    private.readiness_unavailable = True

    who = client.get("/copresenter/who")
    private.readiness_unavailable = False
    private.answer_unavailable = True
    answer = ask(client)
    private.hearing_unavailable = True
    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
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
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    unknown_field = ask(client, {**QUESTION, "cookie": "must not travel"})
    wrong_method = client.put("/copresenter/who")
    unknown_path = client.get("/copresenter/private")

    assert unknown_field.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert wrong_method.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert unknown_path.status_code == HTTPStatus.NOT_FOUND
    assert private.questions == []


def test_a_session_in_use_is_refused_a_page_past_the_cap() -> None:
    lobby, private, clock = a_copresenter_talk_host()
    lobby.sign_in()
    for _ in range(2):
        clock.advance(SESSION_LIFETIME / 2)
        assert lobby.client.get("/copresenter/who").status_code == HTTPStatus.OK
    clock.advance(timedelta(seconds=1))
    calls_before = private.readiness_calls

    assert lobby.client.get("/copresenter/who").status_code == HTTPStatus.FOUND
    assert private.readiness_calls == calls_before


def test_a_session_in_use_is_refused_the_hearing_past_the_cap() -> None:
    lobby, private, clock = a_copresenter_talk_host()
    lobby.sign_in()
    for _ in range(2):
        clock.advance(SESSION_LIFETIME / 2)
        assert lobby.client.get("/copresenter/who").status_code == HTTPStatus.OK
    clock.advance(timedelta(seconds=1))
    touches_before = lobby.people.sessions.touches

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        lobby.client.websocket_connect(HEARING, headers={"origin": TALK_ORIGIN}),
    ):
        pass

    assert refused.value.code == POLICY_VIOLATION
    assert private.hearing_opens == 0
    assert lobby.people.sessions.touches == touches_before


@pytest.mark.parametrize(
    ("after_the_check", "closed"),
    [
        pytest.param(timedelta(seconds=30), False, id="recheck-exactly-at-the-cap"),
        pytest.param(timedelta(seconds=31), True, id="recheck-one-second-past-the-cap"),
    ],
)
def test_a_running_hearing_ends_at_the_cap_on_its_recheck(
    after_the_check: timedelta,
    *,
    closed: bool,
) -> None:
    deadline = ManualDeadline()
    lobby, private, clock = a_copresenter_talk_host(delay=deadline.wait)
    lobby.sign_in()
    clock.advance(SESSION_LIFETIME - timedelta(seconds=30))

    with lobby.client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        touches_before = lobby.people.sessions.touches
        clock.advance(after_the_check)
        deadline.release_first()
        if closed:
            socket.send_text("force an observable close if the session stood")
            with pytest.raises(WebSocketDisconnect) as ended:
                socket.receive_json()
            assert ended.value.code == POLICY_VIOLATION
        else:
            socket.send_bytes(b"pcm after the check")
            assert socket.receive_json() == {"text": "gehört", "final": True}

    if closed:
        assert private.hearing_opens == 0
        assert lobby.people.sessions.touches == touches_before
    else:
        assert private.hearing.frames == [b"pcm after the check"]


def test_expired_and_logged_out_sessions_are_refused_before_private_use() -> None:
    expired_lobby, expired_private, clock = a_copresenter_talk_host()
    expired = expired_lobby.client
    expired_lobby.sign_in()
    clock.advance(SESSION_LIFETIME + timedelta(seconds=1))

    assert expired.get("/copresenter/who").status_code == HTTPStatus.FOUND
    assert expired_private.readiness_calls == 0

    logged_out_lobby, logged_out_private, _clock = a_copresenter_talk_host()
    logged_out = logged_out_lobby.client
    logged_out_lobby.sign_in()
    old_cookie = logged_out.cookies[DEFAULT_SESSION_COOKIE_NAME]
    logged_out.post("/logout")
    logged_out.cookies.set(DEFAULT_SESSION_COOKIE_NAME, old_cookie)

    assert logged_out.get("/copresenter/who").status_code == HTTPStatus.FOUND
    assert logged_out_private.readiness_calls == 0


class RefusingUserStore(FakeUserStore):
    """A user store that can lose an account or fail its read boundary."""

    refuse_reads: bool = False
    fail_reads: bool = False

    def get(self, user_id: str) -> User | None:
        if self.fail_reads:
            message = "session store unavailable"
            raise RuntimeError(message)
        found = super().get(user_id)
        return None if self.refuse_reads else found


@pytest.mark.parametrize("failure", ["account-gone", "store-error"])
def test_identity_loss_fails_closed_before_private_use(failure: str) -> None:
    users = RefusingUserStore()
    lobby, private, _clock = a_copresenter_talk_host(users=users)
    client = lobby.client
    lobby.sign_in(role=Role.ADMIN)

    users.refuse_reads = failure == "account-gone"
    users.fail_reads = failure == "store-error"
    if users.fail_reads:
        with pytest.raises(RuntimeError, match="session store unavailable"):
            client.get("/copresenter/who")
    else:
        assert client.get("/copresenter/who").status_code == HTTPStatus.FOUND

    assert private.readiness_calls == 0


def test_session_store_failure_refuses_websocket_admission_before_private_use() -> None:
    users = RefusingUserStore()
    lobby, private, _clock = a_copresenter_talk_host(users=users)
    client = lobby.client
    lobby.sign_in()
    users.fail_reads = True

    with (
        pytest.raises(RuntimeError, match="session store unavailable"),
        client.websocket_connect(
            HEARING,
            headers={"origin": TALK_ORIGIN},
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

    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=lose_store)
    client = lobby.client
    private.hold_answers = True
    lobby.sign_in()

    response = ask(client)

    assert response.status_code == HTTPStatus.OK
    assert "event: text" in response.text
    assert "event: audio" not in response.text
    assert "event: done" not in response.text
    assert private.questions == [
        Question(said="Was ist wichtig?", slide=2, language="de", deck=ASKED)
    ]
    assert private.answer_exits == 1
    assert private.answer_reader_closed_before_context_exit


def test_busy_answer_stops_at_its_persistent_authorization_deadline() -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    event_count = 40
    refuse_after = 2

    def end_session(index: int) -> None:
        if index == refuse_after:
            users.refuse_reads = True
            deadline.release_first()

    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=deadline.wait)
    client = lobby.client
    private.busy_answer_events = event_count
    private.answer_event_hook = end_session
    lobby.sign_in()

    response = ask(client)

    assert response.status_code == HTTPStatus.OK
    assert response.text.count("event: text") < event_count
    assert private.generated_answer_events < event_count
    assert private.answer_exits == 1


@pytest.mark.parametrize("loss", ["account-gone", "store-error"])
def test_busy_local_hearing_stops_before_more_pcm_after_authoritative_loss(
    loss: str,
) -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=deadline.wait)
    client = lobby.client
    private.hearing.events = [
        HearingTranscript(text="eins", final=True),
        HearingTranscript(text="zwei", final=True),
    ]
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        socket.send_bytes(b"before loss")
        assert socket.receive_json()["text"] == "eins"
        frames_before_loss = list(private.hearing.frames)
        users.refuse_reads = loss == "account-gone"
        users.fail_reads = loss == "store-error"
        deadline.release_first()
        socket.send_bytes(b"after loss")
        socket.send_text("force an observable close if the deadline was reset")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing.frames == frames_before_loss
    assert private.hearing.closed == 1


@pytest.mark.parametrize("loss", ["account-gone", "store-error"])
def test_busy_browser_fallback_keeps_the_original_authorization_deadline(
    loss: str,
) -> None:
    users = RefusingUserStore()
    deadline = ManualDeadline()
    lobby, private, _clock = a_copresenter_talk_host(users=users, delay=deadline.wait)
    client = lobby.client
    private.hearing.events = [None]
    lobby.sign_in()

    with client.websocket_connect(
        HEARING,
        headers={"origin": TALK_ORIGIN},
    ) as socket:
        assert deadline.started.wait(timeout=1)
        socket.send_bytes(b"local hearing fails")
        assert socket.receive_json()["error"] == "hearing unavailable"
        users.refuse_reads = loss == "account-gone"
        users.fail_reads = loss == "store-error"
        deadline.release_first()
        socket.send_bytes(b"busy fallback lease")
        socket.send_text("force an observable close if the deadline was reset")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.hearing.frames == [b"local hearing fails"]
