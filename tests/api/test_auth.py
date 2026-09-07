"""The lobby's real routes, driven the way a browser drives them."""

import logging
import re
from datetime import timedelta
from http import HTTPStatus

import pytest

from presentator.api.auth import SESSION_COOKIE
from presentator.application.identity import (
    FAILURE_WINDOW,
    FAILURES_BEFORE_THROTTLE,
    IDLE_WINDOW,
)
from presentator.contracts.models import Account, Role
from tests.api.lobby import (
    ENGLISH,
    TYPED_WORDS,
    USERNAME,
    Lobby,
    a_lobby,
    a_user_store,
)
from tests.application.fakes import ReversibleHasher, UserStoreThatLostTheRace

_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_WINNERS_HASH = "the hash the winning first start stored"
_WRONG_WORDS = "guessed"


def a_store_that_lost_the_race() -> UserStoreThatLostTheRace:
    store = UserStoreThatLostTheRace()
    store.add_first_account(
        Account(
            id="winner",
            username="someone-else",
            role=Role.ADMIN,
            password_hash=_WINNERS_HASH,
        ),
    )
    return store


def test_the_lobby_sends_a_visitor_who_is_not_signed_in_to_the_login(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login"


def test_first_start_creates_the_admin_and_opens_the_lobby(lobby: Lobby) -> None:
    created = lobby.set_up_admin()

    assert created.status_code == HTTPStatus.SEE_OTHER
    assert created.headers["location"] == "/"
    assert USERNAME in lobby.client.get("/").text


def test_the_session_cookie_travels_locked_down(lobby: Lobby) -> None:
    cookie = lobby.set_up_admin().headers["set-cookie"]

    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert f"Max-Age={int(IDLE_WINDOW.total_seconds())}" in cookie
    assert "Secure" not in cookie


def test_an_https_instance_marks_the_session_cookie_secure() -> None:
    lobby = a_lobby(secure_cookies=True)

    assert "Secure" in lobby.set_up_admin().headers["set-cookie"]
    assert "Secure" in lobby.client.post("/logout").headers["set-cookie"]


def test_a_repeated_password_that_differs_creates_no_account(lobby: Lobby) -> None:
    refused = lobby.set_up_admin(repeated="something else")

    assert refused.status_code == HTTPStatus.OK
    assert ENGLISH.setup_passwords_differ in refused.text
    assert lobby.client.get("/setup").status_code == HTTPStatus.OK


@pytest.mark.parametrize("method", ["get", "post"])
def test_first_start_is_gone_once_an_account_exists(
    signed_in_lobby: Lobby,
    method: str,
) -> None:
    answer = (
        signed_in_lobby.client.get("/setup")
        if method == "get"
        else signed_in_lobby.set_up_admin()
    )

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login"


def test_the_login_page_offers_no_action_but_signing_in(lobby: Lobby) -> None:
    page = lobby.client.get("/login").text

    offered = {
        word
        for word in (ENGLISH.login_submit, ENGLISH.setup_submit, ENGLISH.log_out)
        if word in page
    }
    assert offered == {ENGLISH.login_submit}


def test_the_right_password_opens_the_lobby(signed_in_lobby: Lobby) -> None:
    signed_in_lobby.client.post("/logout")

    signed_in = signed_in_lobby.log_in()

    assert signed_in.status_code == HTTPStatus.SEE_OTHER
    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_deactivated_account_hears_the_same_sentence() -> None:
    lobby = a_lobby(
        users=a_user_store(
            Account(
                id="id-of-felix",
                username=USERNAME,
                role=Role.ADMIN,
                password_hash=ReversibleHasher().hash(TYPED_WORDS),
                is_active=False,
            ),
        ),
    )

    refused = lobby.log_in()

    assert refused.status_code == HTTPStatus.OK
    assert ENGLISH.login_refused in refused.text
    assert SESSION_COOKIE not in lobby.client.cookies


@pytest.mark.parametrize(
    ("username", "password"),
    [
        pytest.param("nobody", TYPED_WORDS, id="unknown-name"),
        pytest.param(USERNAME, _WRONG_WORDS, id="wrong-password"),
    ],
)
def test_a_refused_login_says_one_sentence_that_tells_nothing_apart(
    signed_in_lobby: Lobby,
    username: str,
    password: str,
) -> None:
    refused = signed_in_lobby.log_in(username=username, password=password)

    assert refused.status_code == HTTPStatus.OK
    assert ENGLISH.login_refused in refused.text


def test_repeated_failures_throttle_the_right_password_with_the_same_sentence(
    signed_in_lobby: Lobby,
) -> None:
    for _ in range(FAILURES_BEFORE_THROTTLE):
        signed_in_lobby.log_in(password=_WRONG_WORDS)

    throttled = signed_in_lobby.log_in()

    assert throttled.status_code == HTTPStatus.OK
    assert ENGLISH.login_refused in throttled.text


def test_a_login_survives_a_ninety_minute_talk(signed_in_lobby: Lobby) -> None:
    for _ in range(6):
        signed_in_lobby.clock.advance(timedelta(minutes=15))

        assert signed_in_lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_session_left_alone_for_twelve_hours_leads_back_to_the_login(
    signed_in_lobby: Lobby,
) -> None:
    signed_in_lobby.clock.advance(IDLE_WINDOW + timedelta(seconds=1))

    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.FOUND


def test_the_throttle_lets_go_once_the_failures_age_out(
    signed_in_lobby: Lobby,
) -> None:
    for _ in range(FAILURES_BEFORE_THROTTLE):
        signed_in_lobby.log_in(password=_WRONG_WORDS)

    signed_in_lobby.clock.advance(FAILURE_WINDOW + timedelta(seconds=1))

    assert signed_in_lobby.log_in().status_code == HTTPStatus.SEE_OTHER


def test_every_answer_from_the_lobby_refuses_the_browser_cache(
    signed_in_lobby: Lobby,
) -> None:
    assert signed_in_lobby.client.get("/").headers["cache-control"] == "no-store"
    assert signed_in_lobby.client.get("/login").headers["cache-control"] == "no-store"


def test_logging_out_ends_the_session_and_the_old_cookie_cannot_come_back(
    signed_in_lobby: Lobby,
) -> None:
    cookie = signed_in_lobby.client.cookies[SESSION_COOKIE]

    logged_out = signed_in_lobby.client.post("/logout")

    assert logged_out.status_code == HTTPStatus.SEE_OTHER
    assert logged_out.headers["location"] == "/login"
    assert SESSION_COOKIE not in signed_in_lobby.client.cookies
    signed_in_lobby.client.cookies.set(SESSION_COOKIE, cookie)
    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.FOUND


def test_logging_out_without_a_session_still_lands_at_the_login(lobby: Lobby) -> None:
    logged_out = lobby.client.post("/logout")

    assert logged_out.status_code == HTTPStatus.SEE_OTHER
    assert logged_out.headers["location"] == "/login"


def test_the_password_never_reaches_the_log(
    lobby: Lobby,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        lobby.set_up_admin()
        lobby.log_in(password=_WRONG_WORDS)
        lobby.log_in()
        lobby.client.get("/")

    assert TYPED_WORDS not in caplog.text


def test_an_address_the_lobby_does_not_know_still_leads_to_the_login(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/deck/knowledge-fabric")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login"


def test_a_first_start_that_lost_the_race_leads_to_the_login() -> None:
    lobby = a_lobby(users=a_store_that_lost_the_race())

    answer = lobby.set_up_admin()

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login"
    assert SESSION_COOKIE not in lobby.client.cookies


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": "https://another.example"}, id="foreign-origin"),
        pytest.param({"sec-fetch-site": "cross-site"}, id="cross-site-fetch"),
    ],
)
def test_a_first_start_another_site_submitted_is_refused(
    lobby: Lobby,
    headers: dict[str, str],
) -> None:
    refused = lobby.set_up_admin(headers=headers)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert lobby.client.get("/setup").status_code == HTTPStatus.OK


def test_a_login_another_site_submitted_is_refused(signed_in_lobby: Lobby) -> None:
    refused = signed_in_lobby.log_in(headers={"origin": "https://another.example"})

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_a_form_this_instance_served_is_accepted(lobby: Lobby) -> None:
    created = lobby.set_up_admin(headers={"origin": lobby.own_origin()})

    assert created.status_code == HTTPStatus.SEE_OTHER


def test_a_navigation_without_an_origin_from_this_instance_is_accepted(
    lobby: Lobby,
) -> None:
    created = lobby.set_up_admin(headers={"sec-fetch-site": "same-origin"})

    assert created.status_code == HTTPStatus.SEE_OTHER


def test_logging_out_takes_the_cookie_away_with_the_flags_it_was_set_with(
    signed_in_lobby: Lobby,
) -> None:
    cleared = signed_in_lobby.client.post("/logout").headers["set-cookie"]

    assert "HttpOnly" in cleared
    assert "SameSite=lax" in cleared
    assert "Max-Age=0" in cleared


def test_the_three_stylesheets_are_linked_on_the_open_setup_and_login_pages(
    lobby: Lobby,
) -> None:
    for path in ("/setup", "/login"):
        page = lobby.client.get(path).text

        assert '<link rel="stylesheet" href="/static/pico.classless.min.css">' in page
        assert '<link rel="stylesheet" href="/static/ui-tokens.css">' in page
        assert '<link rel="stylesheet" href="/static/lobby.css">' in page


def test_the_three_stylesheets_are_linked_on_the_signed_in_home_page(
    signed_in_lobby: Lobby,
) -> None:
    page = signed_in_lobby.client.get("/").text

    assert '<link rel="stylesheet" href="/static/pico.classless.min.css">' in page
    assert '<link rel="stylesheet" href="/static/ui-tokens.css">' in page
    assert '<link rel="stylesheet" href="/static/lobby.css">' in page


def test_the_theme_files_are_served_without_signing_in(lobby: Lobby) -> None:
    tokens = lobby.client.get("/static/ui-tokens.css")
    leftover = lobby.client.get("/static/lobby.css")
    pico = lobby.client.get("/static/pico.classless.min.css")

    assert tokens.status_code == HTTPStatus.OK
    assert "--canvas:" in tokens.text
    assert "@media (prefers-color-scheme: dark)" in tokens.text
    assert leftover.status_code == HTTPStatus.OK
    assert "--pico-background-color:" in leftover.text
    assert pico.status_code == HTTPStatus.OK
    assert "Pico CSS" in pico.text


def test_an_address_that_only_starts_like_a_stylesheet_still_asks_for_the_login(
    lobby: Lobby,
) -> None:
    walked_out = lobby.client.get("/static/%2e%2e/deck/a-deck/")

    assert walked_out.status_code == HTTPStatus.FOUND
    assert walked_out.headers["location"] == "/login"


def test_no_signed_in_page_carries_a_hex_colour_or_an_inline_style(
    signed_in_lobby: Lobby,
) -> None:
    for path in ("/", "/login"):
        page = signed_in_lobby.client.get(path).text

        assert not _HEX_COLOUR.search(page)
        assert "style=" not in page


def test_the_open_setup_page_carries_no_hex_colour_or_inline_style(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/setup").text

    assert not _HEX_COLOUR.search(page)
    assert "style=" not in page


def test_the_signed_in_header_offers_the_person_and_log_out(
    signed_in_lobby: Lobby,
) -> None:
    page = signed_in_lobby.client.get("/").text

    assert USERNAME in page
    assert ENGLISH.log_out in page


def test_a_signed_out_page_carries_only_the_wordmark(lobby: Lobby) -> None:
    page = lobby.client.get("/login").text

    assert ENGLISH.wordmark in page
    assert ENGLISH.log_out not in page
    assert USERNAME not in page
