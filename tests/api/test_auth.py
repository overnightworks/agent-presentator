"""The lobby's real routes, driven the way a browser drives them."""

import re
from datetime import timedelta
from http import HTTPStatus

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from httpx2 import Response as BrowserAnswer
from webauth.cookies import (
    DEFAULT_SESSION_COOKIE_NAME,
    MAX_SESSION_COOKIE_CHARS,
)
from webauth.middleware.security_headers import (
    CONTENT_TYPE_OPTIONS,
    FRAME_OPTIONS,
    REFERRER_POLICY,
    STRICT_TRANSPORT_SECURITY,
)
from webauth.oidc import PENDING_LOGIN_MAX_AGE_SECONDS

from presentator.api.hooks import hook_address
from presentator.api.legal import DATENSCHUTZ
from presentator.application.decks import DRAFT_MAX_AGE
from presentator.application.identity import (
    SESSION_LIFETIME,
)
from presentator.contracts.decks import talk_address
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    A_BUILT_DECK_SLUG,
    ENGLISH,
    USERNAME,
    Lobby,
    a_content_security_policy,
    a_lobby_app,
    a_lobby_that_claims_no_origin_of_its_own,
    a_signed_in_lobby_with_a_built_deck,
)

_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# The lobby's own policy (`presentator.api.auth`): never the camera, never a
# page's own location, only the co-presenter's microphone.
_MIC_ONLY_PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=()"
# Every one of these answers the same way whatever carried the request: a
# page, a static file, a 404 nothing names, or an exception nothing caught.
_EVERY_SECURITY_HEADER = {
    "cache-control": "no-store",
    "x-content-type-options": CONTENT_TYPE_OPTIONS,
    "x-frame-options": FRAME_OPTIONS,
    "content-security-policy": a_content_security_policy(),
    "referrer-policy": REFERRER_POLICY,
    "permissions-policy": _MIC_ONLY_PERMISSIONS_POLICY,
}


def test_the_lobby_sends_a_visitor_who_is_not_signed_in_to_the_login(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login/oidc?next=%2F"


def test_a_signed_out_browser_keeps_its_requested_path_and_query_at_login(
    lobby: Lobby,
) -> None:
    asked = lobby.client.get("/settings/sources?tab=recent")

    assert asked.status_code == HTTPStatus.FOUND
    assert (
        asked.headers["location"]
        == "/login/oidc?next=%2Fsettings%2Fsources%3Ftab%3Drecent"
    )


@pytest.mark.parametrize(
    ("accept", "status"),
    [
        pytest.param("Text/HTML", HTTPStatus.FOUND, id="html in other case"),
        pytest.param("application/json", HTTPStatus.UNAUTHORIZED, id="json"),
        pytest.param(
            "text/html;Q=0, application/json;q=0.5",
            HTTPStatus.UNAUTHORIZED,
            id="html refused by an uppercase Q",
        ),
    ],
)
def test_a_signed_out_request_is_redirected_or_refused_by_what_it_accepts(
    lobby: Lobby, accept: str, status: HTTPStatus
) -> None:
    answer = lobby.client.get(
        "/settings/sources?tab=recent", headers={"accept": accept}
    )

    assert answer.status_code == status
    assert (answer.headers.get("location") is not None) is (status == HTTPStatus.FOUND)


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


def test_an_ordinary_signed_in_answer_sets_no_cookie(signed_in_lobby: Lobby) -> None:
    answer = signed_in_lobby.client.get("/")

    assert answer.headers.get_list("set-cookie") == []


@pytest.mark.parametrize(
    "unusable_cookie",
    [
        pytest.param("doubled", id="doubled"),
        pytest.param("forged", id="forged-signature"),
        pytest.param("oversized", id="oversized"),
    ],
)
def test_a_cookie_the_library_cannot_read_leads_to_the_login_without_a_store_read(
    signed_in_lobby: Lobby,
    unusable_cookie: str,
) -> None:
    """A sibling subdomain can plant a second cookie under the session's name.

    Two same-named cookies are refused rather than one of them picked, and a
    forged or oversized one never reaches the session store.
    """
    genuine = signed_in_lobby.client.cookies[DEFAULT_SESSION_COOKIE_NAME]
    named = f"{DEFAULT_SESSION_COOKIE_NAME}={genuine}"
    cookie = {
        "doubled": f"{named}; {named}",
        "forged": f"{DEFAULT_SESSION_COOKIE_NAME}={genuine[:-1]}x",
        "oversized": f"{DEFAULT_SESSION_COOKIE_NAME}={'a' * MAX_SESSION_COOKIE_CHARS}b",
    }[unusable_cookie]

    answer = signed_in_lobby.client.get("/", headers={"cookie": cookie})

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login/oidc?next=%2F"
    assert signed_in_lobby.people.sessions.loads == 0


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

    assert ENGLISH.logged_out_title in page
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


def test_a_session_left_alone_for_twelve_hours_still_stands_at_the_cap(
    signed_in_lobby: Lobby,
) -> None:
    signed_in_lobby.clock.advance(SESSION_LIFETIME)

    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_session_left_alone_for_twelve_hours_leads_back_to_the_login(
    signed_in_lobby: Lobby,
) -> None:
    signed_in_lobby.clock.advance(SESSION_LIFETIME + timedelta(seconds=1))

    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.FOUND


def use_every_quarter_hour_until(lobby: Lobby, lifetime: timedelta) -> None:
    for _ in range(lifetime // timedelta(minutes=15)):
        lobby.clock.advance(timedelta(minutes=15))
        assert lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_session_in_use_all_day_still_stands_at_the_cap(
    signed_in_lobby: Lobby,
) -> None:
    use_every_quarter_hour_until(signed_in_lobby, SESSION_LIFETIME)

    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.OK


def test_a_session_in_use_all_day_is_refused_and_not_renewed_just_past_the_cap(
    signed_in_lobby: Lobby,
) -> None:
    use_every_quarter_hour_until(
        signed_in_lobby, SESSION_LIFETIME - timedelta(minutes=15)
    )
    signed_in_lobby.clock.advance(timedelta(minutes=15, seconds=1))
    touches_before = signed_in_lobby.people.sessions.touches

    assert signed_in_lobby.client.get("/").status_code == HTTPStatus.FOUND
    assert signed_in_lobby.people.sessions.touches == touches_before


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


def test_the_permissions_policy_admits_only_the_copresenters_own_microphone(
    signed_in_lobby: Lobby,
) -> None:
    answer = signed_in_lobby.client.get("/")

    assert answer.headers["permissions-policy"] == _MIC_ONLY_PERMISSIONS_POLICY


def test_a_built_talk_carries_the_same_security_headers_as_the_lobby() -> None:
    lobby = a_signed_in_lobby_with_a_built_deck()

    talk = lobby.get(talk_address(A_BUILT_DECK_SLUG))

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


def test_an_address_the_lobby_does_not_know_still_leads_to_the_login(
    lobby: Lobby,
) -> None:
    answer = lobby.client.get("/deck/knowledge-fabric")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"] == "/login/oidc?next=%2Fdeck%2Fknowledge-fabric"


def test_reading_the_hook_address_leads_to_the_login(lobby: Lobby) -> None:
    asked = lobby.client.get(hook_address("talks"))

    assert asked.status_code == HTTPStatus.FOUND
    assert asked.headers["location"] == "/login/oidc?next=%2Fsources%2Ftalks%2Ffetch"


def test_a_form_naming_the_public_host_is_accepted_without_a_fetch_site() -> None:
    lobby = a_lobby_that_claims_no_origin_of_its_own()

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
    assert walked_out.headers["location"] == "/login/oidc?next=%2F"


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


def test_the_datenschutz_says_the_cookies_are_set_at_sign_in_and_not_renewed() -> None:
    document = " ".join(
        "".join(phrase.words for phrase in paragraph)
        for section in DATENSCHUTZ.sections
        for paragraph in section.paragraphs
    )

    assert "__Secure-oidc_id_token_hint" in document
    assert "__Host-oidc_pending_" in document
    assert "werden danach nicht erneuert" in document
    assert "mit jeder angemeldeten Antwort erneuert" not in document
    assert "Keycloak-Subject-ID und Rolle" in document


def test_the_datenschutz_page_carries_the_approved_session_storage_sentence(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/datenschutz").text

    assert (
        "Gespeichert werden Sitzungs-ID, Benutzer-ID, der Zeitpunkt der Anmeldung "
        "und des Ablaufs sowie das Erneuerungs-Token (Refresh-Token), das Keycloak "
        "bei der Anmeldung ausstellt. Es liegt unverschlüsselt in der "
        "Sitzungszeile, ist ohne das Client-Geheimnis von Presentator bei Keycloak "
        "nicht verwendbar und wird mit der Sitzung gelöscht. Das Zugriffstoken der "
        "Anmeldung wird nicht gespeichert."
    ) in page


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


def test_the_datenschutz_leaves_nothing_open_and_names_its_date(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/datenschutz").text

    assert "[offen" not in page
    assert re.search(r"Stand: \d{1,2}\. \w+ \d{4}", page)


def test_the_datenschutz_names_the_hours_the_code_keeps_a_draft_and_a_session(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/datenschutz").text
    draft_hours = int(DRAFT_MAX_AGE.total_seconds()) // 3600
    session_hours = int(SESSION_LIFETIME.total_seconds()) // 3600

    assert f"rund {draft_hours} Stunden nach Erzeugung" in page
    assert f"{session_hours} Stunden" in page
    assert f"Cookies nach der Anmeldung sind {session_hours} Stunden gültig" in page


@pytest.mark.parametrize(
    "disclosure",
    [
        "vier Cookie-Arten",
        "bleibt es, bis seine {pending_minutes} Minuten ablaufen",
        (
            "Ihre Gruppen und, soweit in Keycloak hinterlegt, Ihren Benutzernamen, "
            "Ihren Namen und Ihre E-Mail-Adresse"
        ),
        (
            "Beim Abmelden wird dieses Cookie gelöscht. Ist es dabei noch "
            "vorhanden und verwendbar, gibt Presentator sein ID-Token als "
            "Hinweis an Keycloak weiter, damit Keycloak ohne Rückfrage "
            "abmelden kann. Der Hinweis steht in der Adresse, die der Browser "
            "aufruft, und kann so im Browserverlauf landen."
        ),
        (
            "Presentator beendet beim Abmelden sofort die Sitzung in "
            "Presentator und leitet den Browser zu Keycloak weiter, um dort "
            "auch die Anmeldung zu beenden; Keycloak fragt dabei "
            "gegebenenfalls nach."
        ),
    ],
)
def test_the_datenschutz_discloses_the_cookie_kinds_the_token_and_the_hint_at_logout(
    lobby: Lobby,
    disclosure: str,
) -> None:
    page = lobby.client.get("/datenschutz").text
    session_hours = int(SESSION_LIFETIME.total_seconds()) // 3600
    pending_minutes = PENDING_LOGIN_MAX_AGE_SECONDS // 60

    assert (
        disclosure.format(pending_minutes=pending_minutes, session_hours=session_hours)
        in page
    )


def test_the_datenschutz_says_the_claude_software_keeps_no_conversation_and_no_old_copy(
    lobby: Lobby,
) -> None:
    page = lobby.client.get("/datenschutz").text

    assert "keinen Gesprächsverlauf auf der Festplatte" in page
    assert "Sitzung auf der Festplatte" not in page
    assert "versucht der Dienst es erneut, wenn er geordnet beendet wird" in page
    assert "gelingt auch das nicht oder bricht der Dienst ab" in page
    assert "Einzige Ausnahme" not in page
    assert "die Frage selbst geht weiterhin an Anthropic" in page
    assert "30 Tage" not in page


@pytest.mark.parametrize("stored_client_detail", ["IP-Adresse", "User-Agent"])
def test_the_datenschutz_names_no_ip_address_and_no_user_agent_as_stored(
    lobby: Lobby,
    stored_client_detail: str,
) -> None:
    assert stored_client_detail not in lobby.client.get("/datenschutz").text


@pytest.mark.parametrize(
    "cookie_name", ["__Host-session_id", "__Host-csrf_token", "slidev-"]
)
def test_the_datenschutz_names_every_cookie_and_storage_key(
    lobby: Lobby,
    cookie_name: str,
) -> None:
    assert cookie_name in lobby.client.get("/datenschutz").text


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
