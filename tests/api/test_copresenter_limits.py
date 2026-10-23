"""The ask limits of the co-presenter: size, language, rate, one answer."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, replace
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.api.copresenter import (
    MAX_SAID_LENGTH,
    CoPresenterSurface,
    add_copresenter_routes,
)
from presentator.application.copresenter import AskLimiter, AskTicket
from tests.adapters.agent_service_support import Wire
from tests.api.copresenter_support import (
    A_DECK,
    ASK,
    HANGING_AFTER_THE_FIRST_WORDS,
    NOW,
    QUESTION,
    SPOKEN_QUESTION,
    TALK_ORIGIN,
    WORKLOAD,
    PrivateFakes,
    a_copresenter_talk_host,
    a_use,
    ask,
    the_admins_decks,
)
from tests.api.lobby import A_TALK_ADDRESS, ADMIN, AN_AGENT_SERVICE
from tests.application.fakes import FrozenClock

if TYPE_CHECKING:
    from httpx2 import Response
    from starlette.types import ASGIApp, Message, Scope


@pytest.mark.parametrize(
    "invalid",
    [
        {"said": "x" * (MAX_SAID_LENGTH + 1)},
        {"language": "fr"},
        {"language": "DE"},
    ],
    ids=["too-long", "foreign-language", "upper-case-language"],
)
def test_ask_refuses_an_overlong_or_foreign_language_question_without_a_turn(
    invalid: dict[str, object],
) -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    response = ask(client, {**QUESTION, **invalid})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert private.answers.messages == []


@pytest.mark.parametrize(
    ("allowed", "language_handed_on"),
    [
        ({"said": "x" * MAX_SAID_LENGTH}, "de"),
        ({"language": "en"}, "en"),
        ({"language": None}, "de"),
        ({"language": ""}, "de"),
    ],
    ids=[
        "longest-said",
        "english",
        "no-language-means-the-default",
        "empty-language-means-the-default-never-an-empty-string",
    ],
)
def test_ask_accepts_the_limit_and_the_offered_languages(
    allowed: dict[str, object], language_handed_on: str
) -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()

    response = ask(client, {**QUESTION, **allowed})

    assert response.status_code == HTTPStatus.OK
    assert [language for _text, language in private.speaking.spoken] == [
        language_handed_on
    ]


def test_the_21st_question_in_ten_minutes_waits_until_the_first_one_leaves() -> None:
    limiter_clock = FrozenClock(instant=NOW)
    lobby, private, _clock = a_copresenter_talk_host(
        limiter=AskLimiter(clock=limiter_clock)
    )
    client = lobby.client
    lobby.sign_in()
    for _ in range(20):
        assert ask(client).status_code == HTTPStatus.OK
        limiter_clock.advance(timedelta(seconds=10))

    refused = ask(client)
    limiter_clock.advance(timedelta(seconds=int(refused.headers["retry-after"])))
    allowed_again = ask(client)

    assert refused.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert refused.headers["retry-after"] == "400"
    assert len(private.answers.messages) == 20 + 1
    assert allowed_again.status_code == HTTPStatus.OK


def test_a_second_question_while_the_first_streams_gets_429_and_no_turn() -> None:
    lobby, private, _clock = a_copresenter_talk_host()
    client = lobby.client
    lobby.sign_in()
    second: list[Response] = []

    def ask_again_mid_answer(index: int) -> None:
        if index == 0 and not second:
            second.append(ask(client))

    private.answers.busy_events = 3
    private.answers.event_hook = ask_again_mid_answer

    first = ask(client)
    after = ask(client)

    assert first.status_code == HTTPStatus.OK
    assert [response.status_code for response in second] == [
        HTTPStatus.TOO_MANY_REQUESTS
    ]
    assert second[0].headers["retry-after"] == "1"
    assert after.status_code == HTTPStatus.OK
    assert len(private.answers.messages) == len([first, after])


def test_each_session_has_its_own_question_budget() -> None:
    limiter_clock = FrozenClock(instant=NOW)
    first_lobby, _private, _clock = a_copresenter_talk_host(
        limiter=AskLimiter(clock=limiter_clock)
    )
    first = first_lobby.client
    first_lobby.sign_in()
    other_lobby = first_lobby.another_browser()
    other_lobby.sign_in()
    other = other_lobby.client
    for _ in range(20):
        ask(first)

    assert ask(first).status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert ask(other).status_code == HTTPStatus.OK


@dataclass(frozen=True)
class FixedSession:
    """The admitted session of a browser."""

    session_id: str = "session"
    user_id: str = ADMIN
    is_admin: bool = True


def test_a_disconnect_before_the_answer_streams_still_frees_the_session() -> None:
    limiter = AskLimiter(clock=FrozenClock(instant=NOW))
    private = PrivateFakes()
    app = FastAPI()
    add_copresenter_routes(
        app,
        surface=CoPresenterSurface(
            use=a_use(private, the_admins_decks()),
            limiter=limiter,
            browser_origin=TALK_ORIGIN,
        ),
        admission=lambda _connection: FixedSession(),
    )

    asyncio.run(_ask_and_disconnect_before_the_body_is_sent(app))

    assert private.answers.messages == []
    assert isinstance(limiter.admit(FixedSession().session_id), AskTicket)


async def _ask_and_disconnect_before_the_body_is_sent(app: ASGIApp) -> None:
    messages: list[Message] = [
        {"type": "http.request", "body": json.dumps(QUESTION).encode()},
        {"type": "http.disconnect"},
    ]

    async def receive() -> Message:
        return messages.pop(0) if messages else await asyncio.Future()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            await asyncio.Future()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/copresenter/ask",
        "raw_path": b"/copresenter/ask",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "state": {},
    }

    await app(scope, receive, send)


def _a_hanging_app(
    wire: Wire, limiter: AskLimiter, admission: Callable[[object], FixedSession]
) -> FastAPI:
    app = FastAPI()
    add_copresenter_routes(
        app,
        surface=CoPresenterSurface(
            use=replace(
                a_use(PrivateFakes(), the_admins_decks()),
                answers=ServiceAnswerer(
                    origin=AN_AGENT_SERVICE,
                    workload_token=a_workload_token,
                    transport=wire.transport,
                ),
            ),
            limiter=limiter,
            browser_origin=A_TALK_ADDRESS,
        ),
        admission=admission,
    )
    return app


def test_a_browser_that_leaves_mid_answer_closes_the_services_connection() -> None:
    """Cell 18."""
    wire = Wire(HANGING_AFTER_THE_FIRST_WORDS)
    limiter = AskLimiter(clock=FrozenClock(instant=NOW))
    app = _a_hanging_app(wire, limiter, lambda _connection: FixedSession())

    asyncio.run(_ask_and_leave_after_the_first_words(app))

    assert wire.response_stream.closed.is_set()
    assert isinstance(limiter.admit(FixedSession().session_id), AskTicket)


def test_the_admission_runs_off_the_event_loop_thread() -> None:
    """Decision 5: the session read of the admission belongs to the thread hop."""
    admitted_on: list[int] = []

    def admission(_connection: object) -> FixedSession:
        admitted_on.append(threading.get_ident())
        return FixedSession()

    app = _a_hanging_app(
        Wire(HANGING_AFTER_THE_FIRST_WORDS),
        AskLimiter(clock=FrozenClock(instant=NOW)),
        admission,
    )

    asyncio.run(_ask_and_leave_after_the_first_words(app))

    assert admitted_on
    assert threading.get_ident() not in admitted_on


async def a_workload_token() -> str:
    return WORKLOAD


async def _ask_and_leave_after_the_first_words(app: ASGIApp) -> None:
    first_words_sent = asyncio.Event()
    messages: list[Message] = [
        {
            "type": "http.request",
            "body": json.dumps({**SPOKEN_QUESTION, "deck": A_DECK}).encode(),
        }
    ]

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        await first_words_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_words_sent.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": ASK,
        "raw_path": ASK.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"talk.testserver"),
            (b"content-type", b"application/json"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
