"""The lobby's real routes, driven the way a browser drives them."""

import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from httpx2 import Response as BrowserAnswer
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME
from webauth.middleware.security_headers import (
    CONTENT_TYPE_OPTIONS,
    FRAME_OPTIONS,
    REFERRER_POLICY,
    STRICT_TRANSPORT_SECURITY,
)

from presentator.api.auth import CONTENT_SECURITY_POLICY
from presentator.api.hooks import hook_address
from presentator.api.legal import DATENSCHUTZ
from presentator.application.identity import (
    IDLE_WINDOW,
)
from presentator.contracts.decks import Build, Deck, talk_address
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    ADMIN,
    ENGLISH,
    NOW,
    SOURCE_ID,
    USERNAME,
    GivenDecks,
    Lobby,
    a_configured_source,
    a_lobby,
    a_lobby_app,
    a_lobby_that_claims_no_origin_of_its_own,
    a_signed_in_lobby,
)
from tests.application.fakes import (
    FakeDeckStore,
)

_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_DECK_SLUG = "hello-deck"
_DECK_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
# What `slidev build` of `examples/hello-deck` writes, `--base /deck/hello-deck/`.
_BUILT_TALK = Path(__file__).parent / "built_talk"
_EXPORTED_PDF = Path(__file__).parent / "exported_deck.pdf"
# The lobby's own policy (`presentator.api.auth`): never the camera, never a
# page's own location, only the co-presenter's microphone.
_MIC_ONLY_PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=()"
# Every one of these answers the same way whatever carried the request: a
# page, a static file, a 404 nothing names, or an exception nothing caught.
_EVERY_SECURITY_HEADER = {
    "cache-control": "no-store",
    "x-content-type-options": CONTENT_TYPE_OPTIONS,
    "x-frame-options": FRAME_OPTIONS,
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "referrer-policy": REFERRER_POLICY,
    "permissions-policy": _MIC_ONLY_PERMISSIONS_POLICY,
}


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


def submit_the_logout_form(
    lobby: Lobby,
    headers: dict[str, str],
) -> BrowserAnswer:
    """The one form every signed-in page carries, with the claims a test names."""
    return lobby.client.post(
        "/logout",
        headers={"content-type": "application/x-www-form-urlencoded", **headers},
    )


def test_a_seeded_session_opens_the_lobby(lobby: Lobby) -> None:
    lobby.sign_in()

    opened = lobby.client.get("/")

    assert opened.status_code == HTTPStatus.OK
    assert USERNAME in opened.text


def test_the_session_cookie_travels_locked_down(signed_in_lobby: Lobby) -> None:
    cookie = signed_in_lobby.client.get("/").headers["set-cookie"]

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


@pytest.mark.parametrize("method", ["get", "post"])
def test_there_is_no_first_start_page_to_set_up_an_admin(
    signed_in_lobby: Lobby,
    method: str,
) -> None:
    answer = signed_in_lobby.client.request(method, "/setup")

    assert answer.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("method", ["get", "post"])
def test_there_is_no_first_start_page_for_a_signed_out_reader_either(
    lobby: Lobby,
    method: str,
) -> None:
    answer = lobby.client.request(method, "/setup")

    assert answer.status_code == HTTPStatus.NOT_FOUND


def test_the_login_page_offers_no_form_and_no_password_field(lobby: Lobby) -> None:
    page = lobby.client.get("/login").text

    assert ENGLISH.login_title in page
    assert "<form" not in page
    assert 'type="password"' not in page


def test_a_password_posted_to_the_login_signs_nobody_in(lobby: Lobby) -> None:
    refused = lobby.client.post(
        "/login",
        data={"username": USERNAME, "password": "the words only this test types"},
    )

    assert refused.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert DEFAULT_SESSION_COOKIE_NAME not in lobby.client.cookies


def test_a_login_survives_a_ninety_minute_talk(signed_in_lobby: Lobby) -> None:
    for _ in range(6):
        signed_in_lobby.clock.advance(timedelta(minutes=15))

        assert signed_in_lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_session_left_alone_for_twelve_hours_leads_back_to_the_login(
    signed_in_lobby: Lobby,
) -> None:
    signed_in_lobby.clock.advance(IDLE_WINDOW + timedelta(seconds=1))

    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.FOUND


def test_every_answer_from_the_lobby_refuses_the_browser_cache(
    signed_in_lobby: Lobby,
) -> None:
    assert signed_in_lobby.client.get("/").headers["cache-control"] == "no-store"
    assert signed_in_lobby.client.get("/login").headers["cache-control"] == "no-store"


def test_a_lobby_reached_over_https_sends_strict_transport_security(
    signed_in_lobby: Lobby,
) -> None:
    answer = signed_in_lobby.client.get("/")

    assert answer.headers["strict-transport-security"] == STRICT_TRANSPORT_SECURITY


def test_a_lobby_reached_over_plain_http_sends_no_hsts() -> None:
    lobby = a_lobby(cookie_secure=False)
    lobby.sign_in()

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


def test_an_uncaught_error_still_carries_the_full_set_of_security_headers(
    signed_in_lobby: Lobby,
) -> None:
    def it_breaks() -> Response:
        message = "an error nothing in this route catches"
        raise RuntimeError(message)

    signed_in_lobby.app.add_api_route("/it-breaks", it_breaks, methods=["GET"])
    client = TestClient(
        signed_in_lobby.app,
        base_url=A_BROWSERS_ADDRESS,
        follow_redirects=False,
        headers=A_BROWSERS_HEADERS,
        raise_server_exceptions=False,
    )
    client.cookies.update(signed_in_lobby.client.cookies)

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


def test_an_address_the_lobby_does_not_know_still_leads_to_the_login(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/deck/knowledge-fabric")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login?next=%2Fdeck%2Fknowledge-fabric"


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": "https://another.example"}, id="foreign-origin"),
        pytest.param({"sec-fetch-site": "cross-site"}, id="cross-site-fetch"),
    ],
)
def test_a_form_another_site_submitted_is_refused(headers: dict[str, str]) -> None:
    lobby = a_lobby_that_claims_no_origin_of_its_own()
    lobby.sign_in()

    refused = submit_the_logout_form(lobby, headers)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert lobby.client.get("/").status_code == HTTPStatus.OK


def test_reading_the_hook_address_leads_to_the_login(lobby: Lobby) -> None:
    asked = lobby.client.get(hook_address("talks"))

    assert asked.status_code == HTTPStatus.FOUND
    assert asked.headers["location"] == "/login?next=%2Fsources%2Ftalks%2Ffetch"


def test_a_form_this_instance_served_is_accepted(lobby: Lobby) -> None:
    accepted = submit_the_logout_form(lobby, {"origin": lobby.own_origin()})

    assert accepted.status_code == HTTPStatus.SEE_OTHER


def test_a_navigation_without_an_origin_from_this_instance_is_accepted(
    lobby: Lobby,
) -> None:
    accepted = submit_the_logout_form(lobby, {"sec-fetch-site": "same-origin"})

    assert accepted.status_code == HTTPStatus.SEE_OTHER


@pytest.mark.parametrize("fetch_site", ["same-site", "none"])
def test_a_same_site_or_schemeless_form_post_is_refused(
    signed_in_lobby: Lobby,
    fetch_site: str,
) -> None:
    refused = submit_the_logout_form(
        signed_in_lobby,
        {"sec-fetch-site": fetch_site},
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_form_post_carrying_no_origin_signal_at_all_is_refused() -> None:
    lobby = a_lobby_that_claims_no_origin_of_its_own()

    refused = submit_the_logout_form(lobby, {})

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_logging_out_expires_the_cookie_at_once(signed_in_lobby: Lobby) -> None:
    cleared = signed_in_lobby.client.post("/logout").headers["set-cookie"]

    assert "SameSite=lax" in cleared
    assert "Max-Age=0" in cleared


def test_the_three_stylesheets_are_linked_on_the_open_login_page(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/login").text

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
    for path in ("/", "/login", "/datenschutz"):
        page = signed_in_lobby.client.get(path).text

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


def _links_the_impressum_and_the_datenschutz(page: str) -> bool:
    """Both footer links, the Impressum pointing at the operator's one page."""
    return (
        f'<a href="https://hallucinai.de/impressum">{ENGLISH.footer_impressum}</a>'
        in page
        and f'<a href="/datenschutz">{ENGLISH.footer_datenschutz}</a>' in page
    )


def test_the_datenschutz_opens_without_a_session(lobby: Lobby) -> None:
    answer = lobby.client.get("/datenschutz")

    assert answer.status_code == HTTPStatus.OK
    assert DATENSCHUTZ.title in answer.text
    assert '<a href="/login">' in answer.text


def test_the_datenschutz_renders_every_section_and_links_the_impressum(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/datenschutz").text
    impressum_link = '<a href="https://hallucinai.de/impressum">Impressum</a>'

    assert all(
        f"<h2>{section.heading}</h2>" in page for section in DATENSCHUTZ.sections
    )
    assert f"siehe {impressum_link}." in page
    assert f"die im {impressum_link} genannte Kontaktadresse" in page


@pytest.mark.parametrize("path", ["/login", "/datenschutz"])
def test_a_signed_out_page_links_the_impressum_and_the_datenschutz(
    lobby: Lobby,
    path: str,
) -> None:
    assert _links_the_impressum_and_the_datenschutz(lobby.client.get(path).text)


@pytest.mark.parametrize("path", ["/", "/account", "/datenschutz"])
def test_a_signed_in_page_links_the_impressum_and_the_datenschutz(
    signed_in_lobby: Lobby,
    path: str,
) -> None:
    page = signed_in_lobby.client.get(path).text

    assert ENGLISH.log_out in page
    assert _links_the_impressum_and_the_datenschutz(page)
