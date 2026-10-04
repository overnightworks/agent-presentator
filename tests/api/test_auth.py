"""The lobby's real routes, driven the way a browser drives them."""

import logging
import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME
from webauth.middleware.security_headers import (
    CONTENT_TYPE_OPTIONS,
    FRAME_OPTIONS,
    REFERRER_POLICY,
    STRICT_TRANSPORT_SECURITY,
)
from webauth.policies import default_content_security_policy

from presentator.api.hooks import hook_address
from presentator.application.identity import (
    FAILURE_WINDOW,
    FAILURES_BEFORE_THROTTLE,
    IDLE_WINDOW,
)
from presentator.contracts.decks import Build, Deck, talk_address
from presentator.contracts.models import Account, Role
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    ADMIN,
    ENGLISH,
    NOW,
    SOURCE_ID,
    TYPED_WORDS,
    USERNAME,
    GivenDecks,
    Lobby,
    a_configured_source,
    a_lobby,
    a_lobby_app,
    a_lobby_that_claims_no_origin_of_its_own,
    a_signed_in_lobby,
    a_user_store,
)
from tests.application.fakes import (
    FakeDeckStore,
    ReversibleHasher,
    UserStoreThatLostTheRace,
)

_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_WINNERS_HASH = "the hash the winning first start stored"
_WRONG_WORDS = "guessed"
_DECK_SLUG = "hello-deck"
_DECK_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
# What `slidev build` of `examples/hello-deck` writes, `--base /deck/hello-deck/`.
_BUILT_TALK = Path(__file__).parent / "built_talk"
_EXPORTED_PDF = Path(__file__).parent / "exported_deck.pdf"
# The lobby's own policy (`presentator.api.auth`): never the camera, never a
# page's own location, only the co-presenter's microphone.
_MIC_ONLY_PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=()"
# `default_content_security_policy([])` is the exact call `presentator.api.auth`
# makes: no script hash ever widens it, the way an inline build output would.
_EXPECTED_CSP = default_content_security_policy([])
# Every one of these answers the same way whatever carried the request: a
# page, a static file, a 404 nothing names, or an exception nothing caught.
_EVERY_SECURITY_HEADER = {
    "cache-control": "no-store",
    "x-content-type-options": CONTENT_TYPE_OPTIONS,
    "x-frame-options": FRAME_OPTIONS,
    "content-security-policy": _EXPECTED_CSP,
    "referrer-policy": REFERRER_POLICY,
    "permissions-policy": _MIC_ONLY_PERMISSIONS_POLICY,
}


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


def a_signed_in_lobby_with_a_built_deck() -> TestClient:
    """A signed-in lobby whose one deck already has a built talk to serve."""
    store = FakeDeckStore()
    store.put(
        Deck(
            slug=_DECK_SLUG,
            title="Hello Co-Presenter",
            changed_at=NOW,
            owner_id=ADMIN,
            source_id=SOURCE_ID,
            commit=_DECK_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    store.put_build(
        _DECK_SLUG,
        Build(
            directory=_BUILT_TALK,
            pdf=_EXPORTED_PDF,
            commit=_DECK_COMMIT,
            built_at=NOW,
        ),
    )
    return a_signed_in_lobby(
        GivenDecks(store=store, source=a_configured_source("git@heimserver:decks.git")),
    )


def test_the_lobby_sends_a_visitor_who_is_not_signed_in_to_the_login(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login?next=%2F"


def test_a_signed_out_browser_keeps_its_requested_path_and_query_at_login(
    lobby: Lobby,
) -> None:
    asked = lobby.client.get("/settings/sources?tab=recent")

    assert asked.status_code == HTTPStatus.FOUND
    assert (
        asked.headers["location"] == "/login?next=%2Fsettings%2Fsources%3Ftab%3Drecent"
    )


def test_a_signed_out_json_request_is_refused_without_a_redirect(lobby: Lobby) -> None:
    refused = lobby.client.get(
        "/settings/sources?tab=recent",
        headers={"accept": "application/json"},
    )

    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    assert refused.headers.get("location") is None


def test_an_unlisted_mutating_route_refuses_a_foreign_form() -> None:
    lobby, _clock = a_lobby_app()

    def a_new_write() -> Response:
        return Response(status_code=HTTPStatus.NO_CONTENT)

    lobby.add_api_route("/a-new-write", a_new_write, methods=["POST"])
    refused = TestClient(lobby).post(
        "/a-new-write",
        headers={"origin": "https://another.example"},
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN


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
    assert "Secure" in cookie


def test_a_doubled_session_cookie_leads_to_the_login(signed_in_lobby: Lobby) -> None:
    """Two same-named cookies are refused rather than one of them picked.

    A sibling subdomain can plant a second cookie under the session's name;
    choosing either would let it decide whose account the request runs in.
    """
    genuine = signed_in_lobby.client.cookies[DEFAULT_SESSION_COOKIE_NAME]
    doubled = f"{DEFAULT_SESSION_COOKIE_NAME}={genuine}"

    answer = signed_in_lobby.client.get(
        "/",
        headers={"cookie": f"{doubled}; {doubled}"},
    )

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login?next=%2F"


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
    assert DEFAULT_SESSION_COOKIE_NAME not in lobby.client.cookies


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
    refusal = signed_in_lobby.log_in(password=_WRONG_WORDS)

    assert signed_in_lobby.client.get("/").headers["cache-control"] == "no-store"
    assert signed_in_lobby.client.get("/login").headers["cache-control"] == "no-store"
    assert refusal.headers["cache-control"] == "no-store"


def test_a_lobby_reached_over_https_sends_strict_transport_security(
    signed_in_lobby: Lobby,
) -> None:
    answer = signed_in_lobby.client.get("/")

    assert answer.headers["strict-transport-security"] == STRICT_TRANSPORT_SECURITY


def test_a_lobby_reached_over_plain_http_sends_no_hsts() -> None:
    lobby = a_lobby(cookie_secure=False)
    lobby.set_up_admin()

    answer = lobby.client.get("/")

    assert "strict-transport-security" not in answer.headers


def test_the_permissions_policy_admits_only_the_copresenters_own_microphone(
    signed_in_lobby: Lobby,
) -> None:
    answer = signed_in_lobby.client.get("/")

    assert answer.headers["permissions-policy"] == _MIC_ONLY_PERMISSIONS_POLICY


def test_a_built_talk_carries_the_same_security_headers_as_the_lobby() -> None:
    lobby = a_signed_in_lobby_with_a_built_deck()

    talk = lobby.get(talk_address(_DECK_SLUG))

    assert talk.status_code == HTTPStatus.OK
    assert talk.headers["strict-transport-security"] == STRICT_TRANSPORT_SECURITY
    for name, value in _EVERY_SECURITY_HEADER.items():
        assert talk.headers[name] == value


@pytest.mark.parametrize(
    "path",
    ["/", "/static/lobby.css", "/an/address/nothing/names"],
    ids=["a signed-in page", "a static file", "a handled 404"],
)
def test_every_real_answer_carries_the_full_set_of_security_headers(
    signed_in_lobby: Lobby,
    path: str,
) -> None:
    answer = signed_in_lobby.client.get(path)

    for name, value in _EVERY_SECURITY_HEADER.items():
        assert answer.headers[name] == value


def test_an_uncaught_error_still_carries_the_full_set_of_security_headers() -> None:
    """Prove the gap a direct ASGI probe once found: every header, still.

    `ServerErrorMiddleware` stands outside every middleware this app added,
    `SecurityHeadersMiddleware` among them: only `auth.py`'s own `Exception`
    handler keeps an unhandled 500 inside that boundary. A direct ASGI probe
    before that handler existed returned a 500 with every one of these
    headers absent.
    """
    lobby, _clock = a_lobby_app()

    def it_breaks() -> Response:
        message = "an error nothing in this route catches"
        raise RuntimeError(message)

    lobby.add_api_route("/it-breaks", it_breaks, methods=["GET"])
    client = TestClient(
        lobby,
        base_url=A_BROWSERS_ADDRESS,
        follow_redirects=False,
        headers=A_BROWSERS_HEADERS,
        raise_server_exceptions=False,
    )
    client.post(
        "/setup",
        data={
            "username": USERNAME,
            "password": TYPED_WORDS,
            "repeated_password": TYPED_WORDS,
        },
    )

    answer = client.get("/it-breaks")

    assert answer.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    for name, value in _EVERY_SECURITY_HEADER.items():
        assert answer.headers[name] == value


def test_logging_out_ends_the_session_and_the_old_cookie_cannot_come_back(
    signed_in_lobby: Lobby,
) -> None:
    cookie = signed_in_lobby.client.cookies[DEFAULT_SESSION_COOKIE_NAME]

    logged_out = signed_in_lobby.client.post("/logout")

    assert logged_out.status_code == HTTPStatus.SEE_OTHER
    assert logged_out.headers["location"] == "/login"
    assert DEFAULT_SESSION_COOKIE_NAME not in signed_in_lobby.client.cookies
    signed_in_lobby.client.cookies.set(DEFAULT_SESSION_COOKIE_NAME, cookie)
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
    assert answer.headers["location"] == "/login?next=%2Fdeck%2Fknowledge-fabric"


def test_a_first_start_that_lost_the_race_leads_to_the_login() -> None:
    lobby = a_lobby(users=a_store_that_lost_the_race())

    answer = lobby.set_up_admin()

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login"
    assert DEFAULT_SESSION_COOKIE_NAME not in lobby.client.cookies


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": "https://another.example"}, id="foreign-origin"),
        pytest.param({"sec-fetch-site": "cross-site"}, id="cross-site-fetch"),
    ],
)
def test_a_first_start_another_site_submitted_is_refused(
    headers: dict[str, str],
) -> None:
    lobby = a_lobby_that_claims_no_origin_of_its_own()

    refused = lobby.set_up_admin(headers=headers)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert lobby.client.get("/setup").status_code == HTTPStatus.OK


def test_a_login_another_site_submitted_is_refused() -> None:
    lobby = a_lobby_that_claims_no_origin_of_its_own()
    lobby.set_up_admin()

    refused = lobby.log_in(headers={"origin": "https://another.example"})

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_reading_the_hook_address_leads_to_the_login(lobby: Lobby) -> None:
    asked = lobby.client.get(hook_address("talks"))

    assert asked.status_code == HTTPStatus.FOUND
    assert asked.headers["location"] == "/login?next=%2Fsources%2Ftalks%2Ffetch"


def test_a_form_this_instance_served_is_accepted(lobby: Lobby) -> None:
    created = lobby.set_up_admin(headers={"origin": lobby.own_origin()})

    assert created.status_code == HTTPStatus.SEE_OTHER


def test_a_navigation_without_an_origin_from_this_instance_is_accepted(
    lobby: Lobby,
) -> None:
    created = lobby.set_up_admin(headers={"sec-fetch-site": "same-origin"})

    assert created.status_code == HTTPStatus.SEE_OTHER


@pytest.mark.parametrize("fetch_site", ["same-site", "none"])
def test_a_same_site_or_schemeless_form_post_is_refused(
    lobby: Lobby,
    fetch_site: str,
) -> None:
    refused = lobby.set_up_admin(headers={"sec-fetch-site": fetch_site})

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert lobby.client.get("/setup").status_code == HTTPStatus.OK


def test_a_form_post_carrying_no_origin_signal_at_all_is_refused() -> None:
    lobby = a_lobby_that_claims_no_origin_of_its_own()

    refused = lobby.set_up_admin()

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_logging_out_expires_the_cookie_at_once(signed_in_lobby: Lobby) -> None:
    cleared = signed_in_lobby.client.post("/logout").headers["set-cookie"]

    assert "SameSite=lax" in cleared
    assert "Max-Age=0" in cleared


def test_the_three_stylesheets_are_linked_on_the_open_setup_and_login_pages(
    lobby: Lobby,
) -> None:
    for path in ("/setup", "/login"):
        page = lobby.client.get(path).text

        assert '<link rel="stylesheet" href="/static/pico.classless.min.css">' in page
        assert '<link rel="stylesheet" href="/static/ui-console.css">' in page
        assert '<link rel="stylesheet" href="/static/lobby.css">' in page


def test_the_three_stylesheets_are_linked_on_the_signed_in_home_page(
    signed_in_lobby: Lobby,
) -> None:
    page = signed_in_lobby.client.get("/").text

    assert '<link rel="stylesheet" href="/static/pico.classless.min.css">' in page
    assert '<link rel="stylesheet" href="/static/ui-console.css">' in page
    assert '<link rel="stylesheet" href="/static/lobby.css">' in page


def test_the_theme_files_are_served_without_signing_in(lobby: Lobby) -> None:
    console = lobby.client.get("/static/ui-console.css")
    leftover = lobby.client.get("/static/lobby.css")
    pico = lobby.client.get("/static/pico.classless.min.css")

    assert console.status_code == HTTPStatus.OK
    assert "--canvas:" in console.text
    assert "@media (prefers-color-scheme: dark)" in console.text
    assert leftover.status_code == HTTPStatus.OK
    assert "--pico-background-color:" in leftover.text
    assert pico.status_code == HTTPStatus.OK
    assert "Pico CSS" in pico.text


def test_an_address_that_only_starts_like_a_stylesheet_still_asks_for_the_login(
    lobby: Lobby,
) -> None:
    walked_out = lobby.client.get("/static/%2e%2e/deck/a-deck/")

    assert walked_out.status_code == HTTPStatus.FOUND
    assert (
        walked_out.headers["location"]
        == "/login?next=%2Fstatic%2F..%2Fdeck%2Fa-deck%2F"
    )


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
