"""The Host header alone chooses which app of the instance answers."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from webauth.config import installed_web_auth_config
from webauth.oidc import OidcClientConfig, OidcRelyingParty

from presentator.adapters.identity import SqliteSessionRecordStore
from presentator.api.talk import TALK_PERMISSIONS_POLICY, talk_content_security_policy
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Role, User
from presentator.host import main
from presentator.host.config import load_settings
from tests.api.fake_issuer import FakeIssuer
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    A_TALK_ADDRESS,
    hand_over_session_cookies,
)
from tests.api.talk_support import (
    A_DECK,
    B_DECK,
    Hosts,
    ask_at_the_door,
    cookie_header,
    two_hosts,
)
from tests.people import seed_user

_LOBBY_HOST = str(urlsplit(A_BROWSERS_ADDRESS).hostname)
_TALK_HOST = str(urlsplit(A_TALK_ADDRESS).hostname)
_SOMEONE_ELSES = "elsewhere.example.test"
_NOT_FOUND = HTTPStatus.NOT_FOUND
_JSON = {"accept": "application/json"}
_ADMIN = User(id="id-of-felix", username="felix", role=Role.ADMIN)


@pytest.fixture
def hosts(tmp_path: Path) -> Hosts:
    return two_hosts(tmp_path)


def _is_the_talk_host(permissions_policy: str | None) -> bool:
    return permissions_policy == TALK_PERMISSIONS_POLICY


# T4a.
def test_the_router_gives_each_hostname_its_app_and_any_other_a_not_found(
    hosts: Hosts,
) -> None:
    lobby_login = hosts.browser(A_BROWSERS_ADDRESS).get("/login")
    talk_root = hosts.browser(A_TALK_ADDRESS).get("/")
    stranger = hosts.browser(f"https://{_SOMEONE_ELSES}").get("/login")

    assert lobby_login.status_code == HTTPStatus.OK
    assert talk_root.status_code == HTTPStatus.SEE_OTHER
    assert talk_root.headers["location"] == A_BROWSERS_ADDRESS
    assert stranger.status_code == _NOT_FOUND


# T4a.
def test_a_socket_for_a_hostname_the_instance_does_not_serve_is_refused(
    hosts: Hosts,
) -> None:
    stranger = hosts.browser(f"https://{_SOMEONE_ELSES}")

    with (
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
        stranger.websocket_connect(f"wss://{_SOMEONE_ELSES}/deck/{A_DECK}/"),
    ):
        pytest.fail("a socket was accepted")


# T4b.
def test_a_scope_with_no_host_header_is_a_not_found(hosts: Hosts) -> None:
    answer = ask_at_the_door(hosts.router, hosts=(), path="/login")

    assert answer.status == _NOT_FOUND


# T4c.
@pytest.mark.parametrize(
    ("first", "second", "answered_by_talk"),
    [(_TALK_HOST, _LOBBY_HOST, True), (_LOBBY_HOST, _TALK_HOST, False)],
    ids=["talk first", "lobby first"],
)
def test_with_two_host_headers_only_the_first_hosts_app_answers(
    hosts: Hosts,
    first: str,
    second: str,
    answered_by_talk: bool,  # noqa: FBT001
) -> None:
    answer = ask_at_the_door(hosts.router, hosts=(first, second), path="/")

    assert (
        _is_the_talk_host(answer.headers.get("permissions-policy")) is answered_by_talk
    )


# T4d.
def test_a_port_on_the_talk_host_never_reaches_the_lobbys_pages(hosts: Hosts) -> None:
    ported = hosts.browser(f"{A_TALK_ADDRESS}:8443")

    settings = ported.get("/settings")

    assert settings.headers["location"].startswith("/login/oidc?")
    assert _is_the_talk_host(settings.headers["permissions-policy"])


# T4d.
def test_a_port_on_the_lobby_never_gets_a_talk_host_answer(hosts: Hosts) -> None:
    ported = hosts.browser_with_the_cookies_of(
        hosts.signed_in_at(A_BROWSERS_ADDRESS), f"{A_BROWSERS_ADDRESS}:8443"
    )

    page = ported.get(f"/deck/{A_DECK}")

    assert page.status_code == HTTPStatus.OK
    assert not _is_the_talk_host(page.headers.get("permissions-policy"))


# T4e.
@pytest.mark.parametrize(
    "target",
    [f"{A_BROWSERS_ADDRESS}/settings", "/settings"],
    ids=["absolute-form as the path (h11)", "origin-form (httptools)"],
)
def test_an_absolute_form_target_naming_the_lobby_is_the_talk_hosts_not_found(
    hosts: Hosts,
    target: str,
) -> None:
    client = hosts.signed_in_at(A_TALK_ADDRESS)

    answer = ask_at_the_door(
        hosts.router, hosts=(_TALK_HOST,), path=target, cookie=cookie_header(client)
    )

    assert answer.status == _NOT_FOUND
    assert _is_the_talk_host(answer.headers["permissions-policy"])


# T4f.
@pytest.mark.parametrize("slug", [A_DECK, f"{B_DECK}/presenter"])
@pytest.mark.parametrize("signed_in", [True, False], ids=["with a session", "none"])
def test_a_socket_to_a_talk_is_closed_before_it_is_accepted(
    hosts: Hosts,
    slug: str,
    signed_in: bool,  # noqa: FBT001
) -> None:
    browser = (
        hosts.signed_in_at(A_TALK_ADDRESS)
        if signed_in
        else hosts.browser(A_TALK_ADDRESS)
    )

    with (
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
        browser.websocket_connect(f"wss://{_TALK_HOST}/deck/{slug}/"),
    ):
        pytest.fail("a socket was accepted")


# T13, T14a and T5c, through the composition root's own wiring.
@pytest.mark.usefixtures("environment")
def test_the_composition_root_wires_the_talk_host_to_its_own_policy() -> None:
    settings = load_settings()
    router = main.served_by_host(main.build_instance(settings), settings)
    talk = TestClient(router, base_url=settings.talk_origin, follow_redirects=False)

    root = talk.get("/")
    from_the_lobby = talk.post(
        "/logout",
        headers={"origin": settings.public_origin, "sec-fetch-site": "same-site"},
    )
    from_the_talk_host = talk.post("/logout", headers={"origin": settings.talk_origin})

    assert root.status_code == HTTPStatus.SEE_OTHER
    assert root.headers["location"] == settings.public_origin
    assert root.headers["content-security-policy"] == talk_content_security_policy()
    assert root.headers["permissions-policy"] == TALK_PERMISSIONS_POLICY
    assert "microphone=(self)" in TALK_PERMISSIONS_POLICY
    assert from_the_lobby.status_code == HTTPStatus.FORBIDDEN
    assert from_the_talk_host.status_code != HTTPStatus.FORBIDDEN


# C1, through the composition root's own wiring.
@pytest.mark.usefixtures("environment")
def test_the_composition_root_serves_the_copresenter_at_the_talk_host_only() -> None:
    settings = load_settings()
    instance = main.build_instance(settings)
    router = main.served_by_host(instance, settings)
    seed_user(settings.database, _ADMIN)
    now = datetime.now(UTC)
    session = SqliteSessionRecordStore(settings.database).create(
        _ADMIN.id,
        now=now,
        expires_at=now + SESSION_LIFETIME,
        refresh_token=None,
        id_token=SecretStr("an-id-token"),
    )
    lobby = TestClient(router, base_url=settings.public_origin)
    talk = TestClient(router, base_url=settings.talk_origin)
    hand_over_session_cookies(
        lobby, session.id, installed_web_auth_config(instance.lobby)
    )
    hand_over_session_cookies(
        talk, session.id, installed_web_auth_config(instance.talk)
    )
    hearing = f"wss://{_LOBBY_HOST}/copresenter/hear?language=de"

    at_the_lobby = lobby.get("/copresenter/who", headers=_JSON)
    at_the_talk_host = talk.get("/copresenter/who", headers=_JSON)
    with (
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
        lobby.websocket_connect(hearing, headers={"origin": settings.public_origin}),
    ):
        pytest.fail("the lobby accepted a hearing")

    assert at_the_lobby.status_code == _NOT_FOUND
    assert at_the_talk_host.status_code == HTTPStatus.SERVICE_UNAVAILABLE


# T1, through the composition root's own wiring.
@pytest.mark.usefixtures("environment")
def test_a_sign_in_at_the_talk_host_stores_no_id_token_while_one_at_the_lobby_does() -> (
    None
):
    """#428 cell 29 (D4, T1): the lobby's row keeps the ID token, the talk host's none."""
    settings = load_settings()
    now = datetime.now(UTC)
    issuer = FakeIssuer(
        issuer=settings.oidc_issuer, client_id=settings.oidc_client_id, now=now
    )

    def relying_party_over_the_fake_issuer(
        config: OidcClientConfig,
    ) -> OidcRelyingParty:
        return OidcRelyingParty(
            config,
            post_token_request=issuer.post_token_request,
            fetch_key_set=issuer.fetch_key_set,
            wall_clock=now.timestamp,
            monotonic_clock=lambda: 0.0,
        )

    router = main.served_by_host(
        main.build_instance(settings, relying_party_over_the_fake_issuer), settings
    )

    def stored_id_tokens() -> list[str | None]:
        with closing(sqlite3.connect(settings.database)) as connection:
            return [
                row[0] for row in connection.execute("SELECT id_token FROM sessions")
            ]

    def sign_in_at(origin: str) -> None:
        browser = TestClient(
            router,
            base_url=origin,
            follow_redirects=False,
            headers=A_BROWSERS_HEADERS,
        )
        started = browser.get("/login/oidc").headers["location"]
        query = issuer.authorize(
            started, subject="subject-1", groups=["/apps/presentator"], username="felix"
        )
        assert (
            browser.get(f"/auth/callback?{query}").status_code == HTTPStatus.SEE_OTHER
        )

    sign_in_at(settings.talk_origin)
    assert stored_id_tokens() == [None]

    sign_in_at(settings.public_origin)
    assert sorted(token is None for token in stored_id_tokens()) == [False, True]
