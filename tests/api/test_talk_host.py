"""The talk host signs people in by itself, and the lobby shows what it refused."""

from http import HTTPStatus

import pytest

from tests.api.lobby import A_TALK_ADDRESS, ENGLISH, Lobby

_IMPRESSUM = f'<a href="https://hallucinai.de/impressum">{ENGLISH.footer_impressum}</a>'
_DATENSCHUTZ = f'<a href="/datenschutz">{ENGLISH.footer_datenschutz}</a>'


@pytest.fixture(params=["signed out", "signed in"])
def either_visitor(request: pytest.FixtureRequest, lobby: Lobby) -> Lobby:
    if request.param == "signed in":
        lobby.sign_in()
    return lobby


@pytest.mark.parametrize(
    ("state", "title", "explanation"),
    [
        ("no-access", ENGLISH.no_access_title, ENGLISH.no_access_explanation),
        ("login-failed", ENGLISH.login_failed_title, ENGLISH.login_failed_explanation),
    ],
)
def test_the_lobby_shows_a_refused_talk_sign_in_alike_with_and_without_a_session(
    either_visitor: Lobby,
    state: str,
    title: str,
    explanation: str,
) -> None:
    page = either_visitor.client.get(f"/login/talk/{state}")

    assert page.status_code == HTTPStatus.FORBIDDEN
    assert f"<h1>{title}</h1>" in page.text
    assert explanation in page.text
    assert _IMPRESSUM in page.text
    assert _DATENSCHUTZ in page.text


def test_no_access_logs_out_at_the_lobby_and_login_failed_retries_at_the_talk_host(
    lobby: Lobby,
) -> None:
    no_access = lobby.client.get("/login/talk/no-access").text
    login_failed = lobby.client.get("/login/talk/login-failed").text

    assert '<form method="post" action="/logout">' in no_access
    assert f'href="{A_TALK_ADDRESS}/login/oidc"' in login_failed


def test_an_unknown_talk_refusal_is_not_found(signed_in_lobby: Lobby) -> None:
    unknown = signed_in_lobby.client.get("/login/talk/other")

    assert unknown.status_code == HTTPStatus.NOT_FOUND
