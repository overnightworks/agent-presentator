"""Signing in starts at the lobby's own route, which sends the browser to the issuer."""

from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.api.lobby import A_CLIENT_ID, AN_ISSUER, ENGLISH, Lobby, a_lobby

_AUTHORIZATION_ENDPOINT = f"{AN_ISSUER}/protocol/openid-connect/auth"


@pytest.fixture
def lobby() -> Lobby:
    return a_lobby()


def _started(lobby: Lobby, next_path: str | None) -> tuple[str, str]:
    """The authorization URL and the one pending-login cookie the start answers."""
    query = "" if next_path is None else f"?next={next_path}"
    answer = lobby.client.get(f"/login/oidc{query}")
    assert answer.status_code == HTTPStatus.SEE_OTHER
    (cookie,) = answer.headers.get_list("set-cookie")
    return answer.headers["location"], cookie


def test_the_start_sends_the_browser_to_the_issuer_with_pkce_state_and_nonce(
    lobby: Lobby,
) -> None:
    location, _ = _started(lobby, "%2Fsettings")

    address = urlsplit(location)
    parameters = {name: values[0] for name, values in parse_qs(address.query).items()}
    assert f"{address.scheme}://{address.netloc}{address.path}" == (
        _AUTHORIZATION_ENDPOINT
    )
    assert parameters["response_type"] == "code"
    assert parameters["client_id"] == A_CLIENT_ID
    assert parameters["redirect_uri"] == "https://testserver/auth/callback"
    assert parameters["scope"] == "openid profile groups"
    assert parameters["code_challenge_method"] == "S256"
    assert parameters["state"]
    assert parameters["nonce"]
    assert parameters["code_challenge"]


def test_the_start_hands_over_one_host_only_pending_login_cookie(
    lobby: Lobby,
) -> None:
    _, cookie = _started(lobby, None)

    assert cookie.startswith("__Host-")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie
    assert "Max-Age=600" in cookie


def test_the_signed_out_page_offers_one_log_in_button_that_starts_the_sign_in(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/login")

    assert answer.status_code == HTTPStatus.OK
    assert ENGLISH.logged_out_title in answer.text
    assert answer.text.count('data-action data-wide href="/login/oidc">Log in</a>') == 1


def test_the_return_from_the_issuer_is_not_sent_back_to_the_start(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/auth/callback?code=an-unclaimed-code&state=x")

    assert answer.status_code == HTTPStatus.NOT_FOUND
