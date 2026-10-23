"""The co-presenter is the talk host's: its session, token, Origin and admin."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME, DEFAULT_SESSION_COOKIE_NAME

from presentator.api.copresenter import CoPresenterSurface
from presentator.application.copresenter import AskLimiter
from presentator.contracts.models import Role, User
from tests.api.copresenter_support import (
    HEARING,
    POLICY_VIOLATION,
    QUESTION,
    TALK_ORIGIN,
    ManualDeadline,
    PrivateFakes,
    a_copresenter_talk_host,
    a_use,
)
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_TALK_ADDRESS,
    NOW,
    USERNAME,
    a_web_auth,
    hand_over_session_cookies,
)
from tests.api.talk_support import A_DECK, MEMBER, A, Hosts, two_hosts
from tests.application.fakes import FakeDeckStore, FrozenClock

if TYPE_CHECKING:
    from pathlib import Path

_JSON = {"accept": "application/json"}
_ASK = "/copresenter/ask"


def _ask_headers(token: str, **claims: str) -> dict[str, str]:
    return {"x-csrf-token": token, **claims}


def _hear_with_origin(browser: TestClient, origin: str | None) -> int | None:
    """The code the test client reports for a handshake the server denied before accept.

    On the wire that is an HTTP 403; None when the hearing was accepted.
    """
    headers = {} if origin is None else {"origin": origin}
    try:
        with browser.websocket_connect(HEARING, headers=headers):
            return None
    except WebSocketDisconnect as refused:
        return refused.code


# C4.
@pytest.mark.parametrize(
    ("role", "status", "reaches_the_service"),
    [
        pytest.param(Role.ADMIN, HTTPStatus.OK, True, id="admin"),
        pytest.param(Role.USER, HTTPStatus.FORBIDDEN, False, id="role-user"),
    ],
)
def test_who_answers_the_admin_only(
    role: Role,
    status: HTTPStatus,
    reaches_the_service: bool,  # noqa: FBT001
) -> None:
    talk, private, _clock = a_copresenter_talk_host()
    talk.sign_in(role=role)

    answer = talk.client.get("/copresenter/who", headers=_JSON)

    assert answer.status_code == status
    assert (private.speech.readiness_calls == 1) is reaches_the_service


# C5.
def test_ask_streams_for_an_admin_with_the_talk_hosts_own_token() -> None:
    talk, _private, _clock = a_copresenter_talk_host()
    talk.sign_in()
    token = talk.client.cookies[DEFAULT_CSRF_COOKIE_NAME]

    answer = talk.client.post(_ASK, json=QUESTION, headers=_ask_headers(token))

    assert answer.status_code == HTTPStatus.OK


# C5.
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sec-fetch-site": "cross-site"}, id="cross-site"),
        pytest.param(
            {"origin": A_BROWSERS_ADDRESS, "sec-fetch-site": "same-site"},
            id="the-lobby-as-origin",
        ),
    ],
)
def test_a_valid_token_does_not_excuse_a_post_from_another_site(
    claims: dict[str, str],
) -> None:
    talk, private, _clock = a_copresenter_talk_host()
    talk.sign_in()
    token = talk.client.cookies[DEFAULT_CSRF_COOKIE_NAME]

    refused = talk.client.post(
        _ASK, json=QUESTION, headers=_ask_headers(token, **claims)
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert private.answers.messages == []


# C5.
def test_a_same_origin_ask_still_needs_the_token() -> None:
    talk, private, _clock = a_copresenter_talk_host()
    talk.sign_in()

    refused = talk.client.post(_ASK, json=QUESTION)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert private.answers.messages == []


# C5.
def test_the_lobbys_csrf_token_does_not_excuse_an_ask_at_the_talk_host() -> None:
    talk, private, clock = a_copresenter_talk_host()
    session_id = talk.people.open_session_for(
        User(id="id-of-an-admin", username=USERNAME, role=Role.ADMIN)
    )
    hand_over_session_cookies(talk.client, session_id, talk.config)
    at_the_lobby = TestClient(talk.app, base_url=A_BROWSERS_ADDRESS)
    hand_over_session_cookies(at_the_lobby, session_id, a_web_auth(clock))
    lobby_token = at_the_lobby.cookies[DEFAULT_CSRF_COOKIE_NAME]

    refused = talk.client.post(_ASK, json=QUESTION, headers=_ask_headers(lobby_token))

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert private.answers.messages == []


# C6.
@pytest.mark.parametrize(
    ("origin", "denied_with"),
    [
        pytest.param(TALK_ORIGIN, None, id="the-talk-origin"),
        pytest.param(A_BROWSERS_ADDRESS, POLICY_VIOLATION, id="the-lobby-origin"),
        pytest.param(None, POLICY_VIOLATION, id="none"),
        pytest.param("https://elsewhere.example.test", POLICY_VIOLATION, id="foreign"),
    ],
)
def test_hear_is_accepted_only_from_the_talk_origin(
    origin: str | None, denied_with: int | None
) -> None:
    talk, _private, _clock = a_copresenter_talk_host()
    talk.sign_in()

    assert _hear_with_origin(talk.client, origin) == denied_with


# C6.
def test_hear_is_refused_to_a_user_even_from_the_talk_origin() -> None:
    talk, private, _clock = a_copresenter_talk_host()
    talk.sign_in(role=Role.USER)

    assert _hear_with_origin(talk.client, TALK_ORIGIN) == POLICY_VIOLATION
    assert private.speech.hearing_opens == 0


# C7.
def test_the_lobbys_session_cookie_does_not_sign_in_at_the_talk_host() -> None:
    talk, private, clock = a_copresenter_talk_host()
    session_id = talk.people.open_session_for(
        User(id="id-of-an-admin", username=USERNAME, role=Role.ADMIN)
    )
    hand_over_session_cookies(talk.client, session_id, a_web_auth(clock))
    assert DEFAULT_SESSION_COOKIE_NAME in talk.client.cookies

    answer = talk.client.get("/copresenter/who", headers=_JSON)

    assert answer.status_code == HTTPStatus.UNAUTHORIZED
    assert private.speech.readiness_calls == 0


# C8.
def test_an_admin_demoted_while_signed_in_loses_the_copresenter_at_once() -> None:
    talk, private, _clock = a_copresenter_talk_host()
    talk.sign_in(role=Role.ADMIN)
    (admin,) = talk.people.users.users.values()
    talk.people.users.users[admin.id] = replace(admin, role=Role.USER)

    answer = talk.client.get("/copresenter/who", headers=_JSON)

    assert answer.status_code == HTTPStatus.FORBIDDEN
    assert private.speech.readiness_calls == 0


def _talk_host_with_a_signed_in_admin(
    tmp_path: Path, deadline: ManualDeadline | None = None
) -> tuple[Hosts, TestClient, PrivateFakes]:
    private = PrivateFakes()
    surface = CoPresenterSurface(
        use=a_use(private, FakeDeckStore()),
        limiter=AskLimiter(clock=FrozenClock(instant=NOW)),
        browser_origin=TALK_ORIGIN,
        delay=asyncio.sleep if deadline is None else deadline.wait,
    )
    hosts = two_hosts(tmp_path, copresenter=lambda _store, _sessions, _auth: surface)
    return hosts, hosts.signed_in_at(A_TALK_ADDRESS), private


def _demote_at_the_lobby(hosts: Hosts) -> None:
    """The admin signs in at the lobby with the issuer's groups no longer admin."""
    lobby = hosts.browser(A_BROWSERS_ADDRESS)
    hosts.sign_in(lobby, A_BROWSERS_ADDRESS, subject=A.id, groups=MEMBER)


# C8.
def test_a_lobby_sign_in_that_demotes_the_admin_keeps_the_deck_and_closes_who(
    tmp_path: Path,
) -> None:
    hosts, talk, private = _talk_host_with_a_signed_in_admin(tmp_path)
    _demote_at_the_lobby(hosts)

    deck = talk.get(f"/deck/{A_DECK}/")
    who = talk.get("/copresenter/who", headers=_JSON)

    assert deck.status_code == HTTPStatus.OK
    assert who.status_code == HTTPStatus.FORBIDDEN
    assert private.speech.readiness_calls == 0


# C8.
def test_a_lobby_sign_in_that_demotes_the_admin_ends_the_running_hearing(
    tmp_path: Path,
) -> None:
    deadline = ManualDeadline()
    hosts, talk, private = _talk_host_with_a_signed_in_admin(tmp_path, deadline)

    with talk.websocket_connect(HEARING, headers={"origin": TALK_ORIGIN}) as socket:
        assert deadline.started.wait(timeout=1)
        socket.send_bytes(b"before demotion")
        assert socket.receive_json() == {"text": "gehört", "final": True}
        _demote_at_the_lobby(hosts)
        deadline.release_first()
        socket.send_text("force an observable close if the hearing stood")
        with pytest.raises(WebSocketDisconnect) as ended:
            socket.receive_json()

    assert ended.value.code == POLICY_VIOLATION
    assert private.speech.hearing.closed == 1


# C9.
@pytest.mark.parametrize(
    ("accept", "status"),
    [
        pytest.param("text/html", HTTPStatus.FOUND, id="page"),
        pytest.param("application/json", HTTPStatus.UNAUTHORIZED, id="json"),
    ],
)
def test_a_signed_out_request_for_a_talk_is_sent_to_sign_in_or_refused(
    tmp_path: Path, accept: str, status: HTTPStatus
) -> None:
    hosts: Hosts = two_hosts(tmp_path)
    browser = hosts.browser(A_TALK_ADDRESS, headers={"accept": accept})

    answer = browser.get(f"/deck/{A_DECK}/")

    assert answer.status_code == status
    if status == HTTPStatus.FOUND:
        assert answer.headers["location"].startswith("/login/oidc?next=")
