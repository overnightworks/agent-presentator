"""The ask limits of the co-presenter: size, language, rate, one answer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME

from presentator.api.copresenter import (
    MAX_SAID_LENGTH,
    CoPresenterSurface,
    add_copresenter_routes,
)
from presentator.application.copresenter import AskLimiter, AskTicket, CoPresenterUse
from tests.api.test_copresenter import (
    NOW,
    PUBLIC_ORIGIN,
    QUESTION,
    RecordingPrivateCoPresenter,
    a_copresenter_lobby,
)
from tests.application.fakes import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi.testclient import TestClient
    from httpx2 import Response
    from starlette.types import ASGIApp, Message, Scope


def ask(client: TestClient, body: Mapping[str, object] = QUESTION) -> Response:
    return client.post(
        "/copresenter/ask",
        json=body,
        headers={"x-csrf-token": client.cookies[DEFAULT_CSRF_COOKIE_NAME]},
    )


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
    lobby, private, _clock = a_copresenter_lobby()
    client = lobby.client
    lobby.sign_in()

    response = ask(client, {**QUESTION, **invalid})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert private.questions == []


@pytest.mark.parametrize(
    ("allowed", "language_handed_on"),
    [
        ({"said": "x" * MAX_SAID_LENGTH}, "de"),
        ({"language": "en"}, "en"),
        ({"language": None}, None),
        ({"language": ""}, None),
    ],
    ids=[
        "longest-said",
        "english",
        "no-language-means-the-default",
        "empty-language-means-the-default-never-an-empty-string",
    ],
)
def test_ask_accepts_the_limit_and_the_offered_languages(
    allowed: dict[str, object], language_handed_on: str | None
) -> None:
    lobby, private, _clock = a_copresenter_lobby()
    client = lobby.client
    lobby.sign_in()

    response = ask(client, {**QUESTION, **allowed})

    assert response.status_code == HTTPStatus.OK
    assert [question.language for question in private.questions] == [language_handed_on]


def test_the_21st_question_in_ten_minutes_waits_until_the_first_one_leaves() -> None:
    limiter_clock = FrozenClock(instant=NOW)
    lobby, private, _clock = a_copresenter_lobby(
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
    assert len(private.questions) == 20 + 1
    assert allowed_again.status_code == HTTPStatus.OK


def test_a_second_question_while_the_first_streams_gets_429_and_no_turn() -> None:
    lobby, private, _clock = a_copresenter_lobby()
    client = lobby.client
    lobby.sign_in()
    second: list[Response] = []

    def ask_again_mid_answer(index: int) -> None:
        if index == 0 and not second:
            second.append(ask(client))

    private.busy_answer_events = 3
    private.answer_event_hook = ask_again_mid_answer

    first = ask(client)
    after = ask(client)

    assert first.status_code == HTTPStatus.OK
    assert [response.status_code for response in second] == [
        HTTPStatus.TOO_MANY_REQUESTS
    ]
    assert second[0].headers["retry-after"] == "1"
    assert after.status_code == HTTPStatus.OK
    assert len(private.questions) == len([first, after])


def test_each_session_has_its_own_question_budget() -> None:
    limiter_clock = FrozenClock(instant=NOW)
    first_lobby, _private, _clock = a_copresenter_lobby(
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
    is_admin: bool = True


def test_a_disconnect_before_the_answer_streams_still_frees_the_session() -> None:
    limiter = AskLimiter(clock=FrozenClock(instant=NOW))
    private = RecordingPrivateCoPresenter()
    app = FastAPI()
    add_copresenter_routes(
        app,
        surface=CoPresenterSurface(
            use=CoPresenterUse(private=private),
            limiter=limiter,
            public_origin=PUBLIC_ORIGIN,
        ),
        admission=lambda _connection: FixedSession(),
    )

    asyncio.run(_ask_and_disconnect_before_the_body_is_sent(app))

    assert private.questions == []
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
