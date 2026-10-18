"""The ask limits of the co-presenter: size, language, rate, one answer."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME

from presentator.api.copresenter import MAX_SAID_LENGTH
from presentator.application.copresenter import AskLimiter
from tests.api.lobby import TYPED_WORDS, USERNAME
from tests.api.test_copresenter import (
    NOW,
    QUESTION,
    a_copresenter_lobby,
    another_browser,
    sign_in,
)
from tests.application.fakes import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi.testclient import TestClient
    from httpx2 import Response


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
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

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
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)

    response = ask(client, {**QUESTION, **allowed})

    assert response.status_code == HTTPStatus.OK
    assert [question.language for question in private.questions] == [language_handed_on]


def test_the_21st_question_in_ten_minutes_waits_until_the_first_one_leaves() -> None:
    limiter_clock = FrozenClock(instant=NOW)
    client, private, _clock = a_copresenter_lobby(
        limiter=AskLimiter(clock=limiter_clock)
    )
    sign_in(client)
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
    client, private, _clock = a_copresenter_lobby()
    sign_in(client)
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
    first, _private, _clock = a_copresenter_lobby(
        limiter=AskLimiter(clock=limiter_clock)
    )
    sign_in(first)
    other = another_browser(first.app)
    other.post("/login", data={"username": USERNAME, "password": TYPED_WORDS})
    for _ in range(20):
        ask(first)

    assert ask(first).status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert ask(other).status_code == HTTPStatus.OK
