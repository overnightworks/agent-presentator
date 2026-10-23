"""The session gate decides every HTTP request, off the event loop."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from http import HTTPStatus

import httpx2
import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME
from webauth.middleware.security_headers import (
    REFERRER_POLICY,
    STRICT_TRANSPORT_SECURITY,
)

from presentator.api.auth import PERMISSIONS_POLICY
from presentator.api.hooks import hook_address
from presentator.api.pages import gated_person, gated_session_id
from presentator.application.identity import SESSION_LIFETIME
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    ENGLISH,
    USERNAME,
    Lobby,
    a_browser_at,
    a_content_security_policy,
    a_crowd,
    a_lobby_app,
)
from tests.application.fakes import FakeSession, FakeSessionRecordStore, FakeUserStore

_JSON = {"accept": "application/json"}
_HTML = {"accept": "text/html"}
_START_SIGN_IN = "/login/oidc"
_PUBLIC_PROBE = "/setup"


@dataclass
class LoopNotingSessionStore(FakeSessionRecordStore):
    """A session store that notes whether an event loop ran during each `touch`."""

    touched_with_a_running_loop: list[bool] = field(default_factory=list[bool])

    def touch(self, session_id: str, *, expires_at: datetime) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.touched_with_a_running_loop.append(False)
        else:
            self.touched_with_a_running_loop.append(True)
        return super().touch(session_id, expires_at=expires_at)


@dataclass
class UnreadableSessionStore(FakeSessionRecordStore):
    """A session store whose read always fails."""

    def load(self, session_id: str) -> FakeSession | None:
        message = f"session store unavailable for {session_id}"
        raise RuntimeError(message)


def a_lobby_over(
    store: Callable[[FakeUserStore], FakeSessionRecordStore],
    *,
    raise_server_exceptions: bool = True,
) -> Lobby:
    """A lobby whose session store is the one the test hands in."""
    users = FakeUserStore()
    people = replace(a_crowd(users), sessions=store(users))
    app, _clock = a_lobby_app(people=people)
    browser = a_browser_at(
        app,
        people=people,
        base_url=A_BROWSERS_ADDRESS,
        headers=A_BROWSERS_HEADERS,
    )
    if raise_server_exceptions:
        return browser
    quiet = TestClient(
        app,
        base_url=A_BROWSERS_ADDRESS,
        follow_redirects=False,
        headers=A_BROWSERS_HEADERS,
        raise_server_exceptions=False,
    )
    return replace(browser, client=quiet)


def _assert_carries_the_security_headers(answer: httpx2.Response) -> None:
    headers = answer.headers
    assert headers["cache-control"] == "no-store"
    assert headers["content-security-policy"] == a_content_security_policy()
    assert headers["permissions-policy"] == PERMISSIONS_POLICY
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == REFERRER_POLICY
    assert headers["strict-transport-security"] == STRICT_TRANSPORT_SECURITY


def _counts(lobby: Lobby) -> tuple[int, int]:
    sessions = lobby.people.sessions
    return sessions.loads, sessions.touches


@pytest.mark.parametrize("cookie", [None, "forged", "ended"])
def test_a_json_call_without_a_live_session_is_refused_without_a_redirect(
    signed_in_lobby: Lobby,
    cookie: str | None,
) -> None:
    client = signed_in_lobby.client
    if cookie == "ended":
        signed_in_lobby.people.sessions.rows.clear()
    elif cookie == "forged":
        client.cookies.set(DEFAULT_SESSION_COOKIE_NAME, "forged")
    else:
        client.cookies.clear()

    refused = client.get("/settings", headers=_JSON)

    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    assert "location" not in refused.headers


def test_a_route_added_after_the_lobby_was_built_still_needs_a_session(
    lobby: Lobby,
) -> None:
    lobby.app.add_api_route("/added-later", lambda: Response("secret"), methods=["GET"])

    answer = lobby.client.get("/added-later")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login/oidc?next=%2Fadded-later"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/login", HTTPStatus.OK),
        ("GET", "/login/oidc", HTTPStatus.SEE_OTHER),
        ("GET", "/setup", HTTPStatus.NOT_FOUND),
        ("POST", "/setup", HTTPStatus.NOT_FOUND),
        ("POST", "/logout", HTTPStatus.OK),
        ("GET", "/static/ui-console.css", HTTPStatus.OK),
    ],
)
def test_a_public_path_answers_a_signed_out_browser_without_a_store_read(
    lobby: Lobby,
    method: str,
    path: str,
    status: HTTPStatus,
) -> None:
    answer = lobby.client.request(method, path)

    assert answer.status_code == status
    assert _counts(lobby) == (0, 0)


@pytest.mark.parametrize("path", ["/setup", "/static/ui-console.css", "/login/oidc"])
def test_a_signed_in_browser_on_a_public_path_costs_no_store_read(
    signed_in_lobby: Lobby,
    path: str,
) -> None:
    signed_in_lobby.client.get(path)

    assert _counts(signed_in_lobby) == (0, 0)


def test_the_sign_in_callback_is_not_turned_to_the_login_by_the_gate(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/auth/callback")

    assert not answer.headers.get("location", "").startswith(_START_SIGN_IN)
    assert _counts(lobby) == (0, 0)


@pytest.mark.parametrize(
    "path", ["/login/extra", "/staticity/x", "/static/%2e/lobby.css"]
)
def test_an_address_beside_a_public_one_is_gated(lobby: Lobby, path: str) -> None:
    answer = lobby.client.get(path)

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"].startswith("/login/oidc?next=")


def test_a_signed_in_browser_reading_the_login_costs_no_store_read(
    signed_in_lobby: Lobby,
) -> None:
    answer = signed_in_lobby.client.get("/login")

    assert answer.status_code == HTTPStatus.OK
    assert ENGLISH.log_out not in answer.text
    assert _counts(signed_in_lobby) == (0, 0)


def test_a_head_request_to_the_hook_address_is_gated(lobby: Lobby) -> None:
    answer = lobby.client.head(hook_address("talks"))

    assert answer.status_code == HTTPStatus.FOUND


@pytest.mark.parametrize(
    ("accept", "status"),
    [(_HTML, HTTPStatus.FOUND), (_JSON, HTTPStatus.UNAUTHORIZED)],
)
def test_a_dot_segment_under_the_hook_prefix_is_refused_before_the_route(
    lobby: Lobby,
    accept: dict[str, str],
    status: HTTPStatus,
) -> None:
    answer = lobby.client.post("/sources/%2e%2e/logout", headers=accept)

    assert answer.status_code == status


def test_the_datenschutz_opens_signed_out_without_a_store_read(lobby: Lobby) -> None:
    answer = lobby.client.get("/datenschutz")

    assert answer.status_code == HTTPStatus.OK
    assert _counts(lobby) == (0, 0)


def test_the_datenschutz_shows_the_person_signed_in_with_one_admission(
    signed_in_lobby: Lobby,
) -> None:
    answer = signed_in_lobby.client.get("/datenschutz")

    assert answer.status_code == HTTPStatus.OK
    assert USERNAME in answer.text
    assert _counts(signed_in_lobby) == (1, 1)


def test_the_datenschutz_reads_as_signed_out_for_a_session_past_the_cap(
    signed_in_lobby: Lobby,
) -> None:
    signed_in_lobby.clock.advance(SESSION_LIFETIME * 2)

    answer = signed_in_lobby.client.get("/datenschutz")

    assert answer.status_code == HTTPStatus.OK
    assert USERNAME not in answer.text


def test_a_signed_in_page_costs_one_load_and_one_touch(signed_in_lobby: Lobby) -> None:
    answer = signed_in_lobby.client.get("/")

    assert answer.status_code == HTTPStatus.OK
    assert _counts(signed_in_lobby) == (1, 1)


def test_the_renewal_runs_on_a_thread_with_no_event_loop() -> None:
    lobby = a_lobby_over(lambda users: LoopNotingSessionStore(users=users))
    lobby.sign_in()

    lobby.client.get("/")

    store = lobby.people.sessions
    assert isinstance(store, LoopNotingSessionStore)
    assert store.touched_with_a_running_loop == [False]


def test_an_unreadable_session_store_runs_no_route_and_keeps_the_headers() -> None:
    lobby = a_lobby_over(
        lambda users: UnreadableSessionStore(users=users),
        raise_server_exceptions=False,
    )
    lobby.sign_in()
    reached: list[str] = []
    lobby.app.add_api_route(
        "/reached", lambda: reached.append("route") or Response(), methods=["GET"]
    )

    answer = lobby.client.get("/reached")

    assert answer.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert reached == []
    _assert_carries_the_security_headers(answer)


@pytest.mark.parametrize("reader", [gated_person, gated_session_id])
@pytest.mark.parametrize("fixture", ["lobby", "signed_in_lobby"])
def test_a_gated_reader_fails_loud_on_a_path_the_gate_let_past(
    request: pytest.FixtureRequest,
    fixture: str,
    reader: Callable[[Request], object],
) -> None:
    lobby: Lobby = request.getfixturevalue(fixture)

    def probe(incoming: Request) -> Response:
        reader(incoming)
        return Response()

    lobby.app.add_api_route(_PUBLIC_PROBE, probe, methods=["POST"])

    with pytest.raises(RuntimeError, match="admitted"):
        lobby.client.post(_PUBLIC_PROBE)


@pytest.mark.parametrize(
    ("accept", "status"),
    [(_HTML, HTTPStatus.FOUND), (_JSON, HTTPStatus.UNAUTHORIZED)],
)
def test_the_gates_refusals_carry_the_security_headers(
    lobby: Lobby,
    accept: dict[str, str],
    status: HTTPStatus,
) -> None:
    refused = lobby.client.get("/", headers=accept)

    assert refused.status_code == status
    _assert_carries_the_security_headers(refused)
