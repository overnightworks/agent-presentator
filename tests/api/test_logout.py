"""Logging out ends the Keycloak session too, and clears every cookie of the sign-in."""

from http import HTTPStatus
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from webauth.cookies import (
    DEFAULT_CSRF_COOKIE_NAME,
    DEFAULT_SESSION_COOKIE_NAME,
    MAX_ID_TOKEN_HINT_CHARS,
)

from presentator.api.auth import LOGIN_PATH, LOGOUT_PATH, content_security_policy
from presentator.host.oidc import end_session_origin, oidc_client_config
from tests.api.lobby import A_CLIENT_CREDENTIAL, a_settings
from tests.test_sign_in_callback import Scene, WatchedLock, a_scene

_HINT_NAME = "id_token_hint"
_A_FORM = {"content-type": "application/x-www-form-urlencoded"}
_UNUSABLE_HINTS = {
    "malformed": "not-a-compact-jws",
    "oversized": f"{'a' * MAX_ID_TOKEN_HINT_CHARS}.b.c",
}
_END_SESSION = oidc_client_config(a_settings(), SecretStr(A_CLIENT_CREDENTIAL))


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return a_scene(tmp_path)


def _issued_hint(scene: Scene) -> Cookie:
    (issued,) = (
        cookie for cookie in scene.lobby.client.cookies.jar if _HINT_NAME in cookie.name
    )
    return issued


def _set_hint(scene: Scene, value: str | None, *, path: str = LOGOUT_PATH) -> None:
    """Swap the hint the sign-in issued for a value of the test's own, or drop it."""
    issued = _issued_hint(scene)
    scene.lobby.client.cookies.jar.clear(issued.domain, issued.path, issued.name)
    if value is not None:
        scene.lobby.client.cookies.set(
            issued.name, value, domain=issued.domain, path=path
        )


def _arrange(scene: Scene, *, session: bool, hint: str) -> str | None:
    """Leave a signed-in browser with the session and hint a cell names.

    Returns the hint value the library is expected to forward, or None.
    """
    forwarded: str | None = None
    issued = _issued_hint(scene)
    assert issued.value is not None
    match hint:
        case "usable":
            forwarded = issued.value
        case "missing":
            _set_hint(scene, None)
        case "duplicated":
            scene.lobby.client.cookies.set(
                issued.name, issued.value, domain=issued.domain, path="/"
            )
        case _:
            _set_hint(scene, _UNUSABLE_HINTS[hint])
    if not session:
        for name in (DEFAULT_SESSION_COOKIE_NAME, DEFAULT_CSRF_COOKIE_NAME):
            del scene.lobby.client.cookies[name]
    return forwarded


def _cleared_paths(set_cookie_headers: list[str]) -> dict[str, str]:
    """Each cookie a logout deleted with Max-Age=0, by exact name, to its path."""
    deletions: dict[str, str] = {}
    for header in set_cookie_headers:
        for name, morsel in SimpleCookie(header).items():
            assert morsel["max-age"] == "0"
            assert name not in deletions
            deletions[name] = morsel["path"]
    return deletions


def _logout(scene: Scene) -> tuple[HTTPStatus, dict[str, list[str]], list[str], str]:
    answer = scene.lobby.client.post(LOGOUT_PATH, headers=_A_FORM)
    location = answer.headers["location"]
    return (
        HTTPStatus(answer.status_code),
        parse_qs(urlsplit(location).query),
        answer.headers.get_list("set-cookie"),
        location,
    )


@pytest.mark.parametrize("session", [True, False], ids=["session", "no-session"])
@pytest.mark.parametrize(
    "hint", ["usable", "missing", "malformed", "oversized", "duplicated"]
)
def test_logging_out_goes_to_the_end_session_endpoint_and_clears_the_sign_in(
    scene: Scene,
    *,
    session: bool,
    hint: str,
) -> None:
    scene.sign_in()
    hint_name = _issued_hint(scene).name
    sent = _arrange(scene, session=session, hint=hint)
    sessions_before = scene.count("sessions")

    status, query, cookies, location = _logout(scene)

    assert status == HTTPStatus.SEE_OTHER
    assert location.startswith(f"{_END_SESSION.end_session_endpoint}?")
    assert query["client_id"] == [_END_SESSION.client_id]
    assert query["post_logout_redirect_uri"] == [_END_SESSION.post_logout_redirect_uri]
    assert urlsplit(query["post_logout_redirect_uri"][0]).path == LOGIN_PATH
    assert query.get("id_token_hint") == (None if sent is None else [sent])
    assert scene.count("sessions") == sessions_before - (1 if session else 0)
    assert _cleared_paths(cookies) == {
        DEFAULT_SESSION_COOKIE_NAME: "/",
        DEFAULT_CSRF_COOKIE_NAME: "/",
        hint_name: LOGOUT_PATH,
    }


def test_the_session_cookie_of_a_logged_out_browser_cannot_come_back(
    scene: Scene,
) -> None:
    scene.sign_in()
    cookie = scene.lobby.client.cookies[DEFAULT_SESSION_COOKIE_NAME]

    scene.lobby.client.post(LOGOUT_PATH, headers=_A_FORM)

    scene.lobby.client.cookies.set(DEFAULT_SESSION_COOKIE_NAME, cookie)
    assert scene.lobby.client.get("/").status_code == HTTPStatus.FOUND


def test_a_logout_form_without_a_token_from_this_origin_is_accepted(
    scene: Scene,
) -> None:
    scene.sign_in()

    accepted = scene.lobby.client.post(
        LOGOUT_PATH,
        headers={**_A_FORM, "origin": scene.lobby.own_origin()},
    )

    assert accepted.status_code == HTTPStatus.SEE_OTHER
    assert scene.count("sessions") == 0


@pytest.mark.parametrize(
    ("headers", "sends_fetch_site"),
    [
        pytest.param(
            {"origin": "https://another.example", "sec-fetch-site": "cross-site"},
            True,
            id="foreign-origin",
        ),
        pytest.param({"sec-fetch-site": "cross-site"}, True, id="cross-site-fetch"),
        pytest.param(
            {"origin": "https://another.example"},
            False,
            id="foreign-origin-without-fetch-site",
        ),
    ],
)
def test_a_logout_form_another_site_submitted_deletes_nothing_and_goes_nowhere(
    scene: Scene,
    headers: dict[str, str],
    *,
    sends_fetch_site: bool,
) -> None:
    scene.sign_in()
    if not sends_fetch_site:
        del scene.lobby.client.headers["sec-fetch-site"]

    refused = scene.lobby.client.post(LOGOUT_PATH, headers={**_A_FORM, **headers})

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert "location" not in refused.headers
    assert scene.count("sessions") == 1


@pytest.mark.parametrize("signed_in", [True, False], ids=["signed-in", "signed-out"])
def test_a_logout_form_with_no_origin_signal_deletes_nothing_and_goes_nowhere(
    scene: Scene,
    *,
    signed_in: bool,
) -> None:
    if signed_in:
        scene.sign_in()
    del scene.lobby.client.headers["sec-fetch-site"]

    refused = scene.lobby.client.post(LOGOUT_PATH, headers=_A_FORM)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert "location" not in refused.headers
    assert scene.count("sessions") == (1 if signed_in else 0)


def test_a_logout_holds_the_write_lock_once_on_a_worker_thread(
    tmp_path: Path,
) -> None:
    lock = WatchedLock()
    scene = a_scene(tmp_path, lock=lock)
    scene.sign_in()
    held_by_the_sign_in = lock.holds

    scene.lobby.client.post(LOGOUT_PATH, headers=_A_FORM)

    assert lock.holds == held_by_the_sign_in + 1
    assert not lock.held_on_the_event_loop


def test_the_policy_lets_a_form_go_to_this_origin_and_the_end_session_origin() -> None:
    origin = end_session_origin(_END_SESSION)

    directives = dict(
        part.strip().split(" ", 1)
        for part in content_security_policy(origin).split(";")
    )

    assert directives["form-action"] == f"'self' {origin}"
    assert urlsplit(origin).path == ""
    assert _END_SESSION.end_session_endpoint.startswith(f"{origin}/")


def test_the_end_session_origin_keeps_a_port_and_drops_the_path() -> None:
    settings = a_settings().model_copy(
        update={"oidc_issuer": "https://auth.example.test:8443/realms/test"}
    )

    origin = end_session_origin(oidc_client_config(settings, SecretStr("s")))

    assert origin == "https://auth.example.test:8443"


def test_every_answer_of_the_lobby_carries_the_end_session_origin_in_form_action(
    scene: Scene,
) -> None:
    answer = scene.lobby.client.get("/login")

    form_action = next(
        part.strip()
        for part in answer.headers["content-security-policy"].split(";")
        if part.strip().startswith("form-action")
    )
    assert form_action.endswith(end_session_origin(_END_SESSION))


def test_the_header_form_and_the_datenschutz_link_name_the_paths_of_the_routes(
    scene: Scene,
) -> None:
    scene.sign_in()
    signed_in_page = scene.lobby.client.get("/").text
    signed_out_page = scene.browser().get("/datenschutz").text

    assert f'action="{LOGOUT_PATH}"' in signed_in_page
    assert f'href="{LOGIN_PATH}"' in signed_out_page
