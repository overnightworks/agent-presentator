"""The talk host signs people in by itself, and the lobby shows what it refused."""

from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME
from webauth.oidc_login import AdmissionConfig

from presentator.api.talk import TALK_PERMISSIONS_POLICY, talk_content_security_policy
from tests.api.lobby import A_BROWSERS_ADDRESS, A_TALK_ADDRESS, ENGLISH, Lobby
from tests.api.talk_support import (
    A_DECK,
    B_DECK,
    MEMBER,
    NO_DECK,
    A,
    Hosts,
    two_hosts,
)
from tests.people import seed_user

_IMPRESSUM = f'<a href="https://hallucinai.de/impressum">{ENGLISH.footer_impressum}</a>'
_DATENSCHUTZ = f'<a href="/datenschutz">{ENGLISH.footer_datenschutz}</a>'


@pytest.fixture(params=["signed out", "signed in"])
def either_visitor(request: pytest.FixtureRequest, lobby: Lobby) -> Lobby:
    if request.param == "signed in":
        lobby.sign_in()
    return lobby


# T9b.
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


# T9b.
def test_no_access_logs_out_at_the_lobby_and_login_failed_retries_at_the_talk_host(
    lobby: Lobby,
) -> None:
    no_access = lobby.client.get("/login/talk/no-access").text
    login_failed = lobby.client.get("/login/talk/login-failed").text

    assert '<form method="post" action="/logout">' in no_access
    assert f'href="{A_TALK_ADDRESS}/login/oidc"' in login_failed


# T9b.
def test_an_unknown_talk_refusal_is_not_found(signed_in_lobby: Lobby) -> None:
    unknown = signed_in_lobby.client.get("/login/talk/other")

    assert unknown.status_code == HTTPStatus.NOT_FOUND


# ---- the talk host itself ----------------------------------------------------

_BUILT_TALK = Path(__file__).parent / "built_talk"
_SIGNED_OUT = "signed out"
_LOBBY_ONLY_PAGES = [
    "/settings",
    "/settings/sources",
    "/datenschutz",
    "/login",
    "/static/lobby.css",
    "/login/talk/no-access",
]
_LOBBY_ONLY_POSTS = [
    "/logout",
    "/auth/backchannel-logout",
    "/settings/sources/new",
    "/sources/decks/fetch",
]
_TALK_ORIGIN_CLAIMS = {"origin": A_TALK_ADDRESS, "sec-fetch-site": "same-origin"}


@pytest.fixture
def hosts(tmp_path: Path) -> Hosts:
    return two_hosts(tmp_path)


def _is_the_talk_host(answer: Response) -> bool:
    return answer.headers.get("permissions-policy") == TALK_PERMISSIONS_POLICY


# T5a.
@pytest.mark.parametrize("path", _LOBBY_ONLY_PAGES)
def test_a_lobby_page_is_not_found_on_the_talk_host(hosts: Hosts, path: str) -> None:
    talk = hosts.signed_in_at(A_TALK_ADDRESS)

    answer = talk.get(path)

    assert answer.status_code == HTTPStatus.NOT_FOUND
    assert _is_the_talk_host(answer)


# T5a.
@pytest.mark.parametrize("path", _LOBBY_ONLY_POSTS)
def test_a_lobby_form_is_not_found_on_the_talk_host(hosts: Hosts, path: str) -> None:
    talk = hosts.signed_in_at(A_TALK_ADDRESS)

    answer = talk.post(path, headers=_TALK_ORIGIN_CLAIMS)

    assert answer.status_code == HTTPStatus.NOT_FOUND


# T5a.
def test_the_deck_page_address_is_only_the_mounts_slash_redirect_on_the_talk_host(
    hosts: Hosts,
) -> None:
    answer = hosts.signed_in_at(A_TALK_ADDRESS).get(f"/deck/{A_DECK}")

    assert answer.status_code == HTTPStatus.TEMPORARY_REDIRECT
    assert answer.headers["location"].endswith(f"/deck/{A_DECK}/")


# T5b.
@pytest.mark.parametrize("path", [*_LOBBY_ONLY_PAGES, f"/deck/{A_DECK}"])
def test_signed_out_the_talk_host_sends_every_page_to_its_own_sign_in(
    hosts: Hosts, path: str
) -> None:
    answer = hosts.browser(A_TALK_ADDRESS).get(path)

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"].startswith("/login/oidc?next=")


# T5b.
@pytest.mark.parametrize("path", _LOBBY_ONLY_POSTS)
def test_signed_out_a_form_to_the_talk_host_is_sent_to_its_sign_in_too(
    hosts: Hosts, path: str
) -> None:
    answer = hosts.browser(A_TALK_ADDRESS).post(path, headers=_TALK_ORIGIN_CLAIMS)

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"].startswith("/login/oidc?next=")


# T5b.
def test_signed_out_a_caller_that_asks_for_json_gets_a_401_from_the_talk_host(
    hosts: Hosts,
) -> None:
    browser = hosts.browser(A_TALK_ADDRESS, headers={"accept": "application/json"})

    assert browser.get("/settings").status_code == HTTPStatus.UNAUTHORIZED


# T5c.
@pytest.mark.parametrize("signed_in", [True, False], ids=["signed in", _SIGNED_OUT])
def test_the_talk_hosts_root_sends_the_browser_to_the_lobby(
    hosts: Hosts,
    signed_in: bool,  # noqa: FBT001
) -> None:
    browser = (
        hosts.signed_in_at(A_TALK_ADDRESS)
        if signed_in
        else hosts.browser(A_TALK_ADDRESS)
    )

    root = browser.get("/")

    assert root.status_code == HTTPStatus.SEE_OTHER
    assert root.headers["location"] == A_BROWSERS_ADDRESS


# ---- signing in at the talk host ---------------------------------------------


# T6.
def test_asking_for_a_talk_signed_out_signs_in_at_the_talk_host_and_lands_on_it(
    hosts: Hosts,
) -> None:
    browser = hosts.browser(A_TALK_ADDRESS)
    presenter = f"/deck/{A_DECK}/presenter/"

    gated = browser.get(presenter)
    returned = hosts.sign_in(browser, A_TALK_ADDRESS, next_path=presenter)
    hosts.give_a_deck_to(A.username)
    opened = browser.get(presenter)

    assert gated.headers["location"] == f"/login/oidc?next={quote(presenter, safe='')}"
    assert returned.status_code == HTTPStatus.SEE_OTHER
    assert returned.headers["location"] == presenter
    assert opened.status_code == HTTPStatus.OK


# T6.
def test_the_talk_host_sends_the_issuer_the_talk_callback_and_a_pkce_challenge(
    hosts: Hosts,
) -> None:
    started = hosts.browser(A_TALK_ADDRESS).get("/login/oidc")

    query = parse_qs(urlsplit(started.headers["location"]).query)
    assert started.status_code == HTTPStatus.SEE_OTHER
    assert query["redirect_uri"] == [f"{A_TALK_ADDRESS}/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"]


# T6.
def test_a_hostile_next_lands_on_the_talk_hosts_root(hosts: Hosts) -> None:
    returned = hosts.sign_in(
        hosts.browser(A_TALK_ADDRESS),
        A_TALK_ADDRESS,
        next_path="https://evil.example.test/",
    )

    assert returned.headers["location"] == "/"


# T7d.
def test_each_host_sends_the_issuer_its_own_callback_when_it_exchanges_the_code(
    hosts: Hosts,
) -> None:
    hosts.sign_in(hosts.browser(A_BROWSERS_ADDRESS), A_BROWSERS_ADDRESS)
    hosts.sign_in(hosts.browser(A_TALK_ADDRESS), A_TALK_ADDRESS)

    assert hosts.issuer.redirect_uris == [
        f"{A_BROWSERS_ADDRESS}/auth/callback",
        f"{A_TALK_ADDRESS}/auth/callback",
    ]


# T8.
def test_the_talk_hosts_cookies_are_host_only_secure_and_its_session_is_lax(
    hosts: Hosts,
) -> None:
    returned = hosts.sign_in(hosts.browser(A_TALK_ADDRESS), A_TALK_ADDRESS)

    cookies = returned.headers.get_list("set-cookie")
    session = [c for c in cookies if c.startswith(f"{DEFAULT_SESSION_COOKIE_NAME}=")]
    assert len(session) == 1
    assert "path=/;" in session[0].lower()
    assert "samesite=lax" in session[0].lower()
    for cookie in cookies:
        assert cookie.startswith(("__Host-", "__Secure-"))
        assert "secure" in cookie.lower()
        assert "domain" not in cookie.lower()


# T7a.
@pytest.mark.parametrize("started_at", [A_BROWSERS_ADDRESS, A_TALK_ADDRESS])
def test_a_session_cookie_of_one_host_is_no_session_at_the_other(
    hosts: Hosts, started_at: str
) -> None:
    other = A_TALK_ADDRESS if started_at == A_BROWSERS_ADDRESS else A_BROWSERS_ADDRESS
    signed_in = hosts.signed_in_at(started_at)
    carried = hosts.browser_with_the_cookies_of(signed_in, other)

    answer = carried.get(f"/deck/{A_DECK}/presenter/")

    assert answer.status_code == HTTPStatus.FOUND
    assert answer.headers["location"].startswith("/login/oidc?next=")


# T7b.
@pytest.mark.parametrize("started_at", [A_BROWSERS_ADDRESS, A_TALK_ADDRESS])
def test_a_pending_login_of_one_host_cannot_be_completed_at_the_other(
    hosts: Hosts, started_at: str
) -> None:
    other = A_TALK_ADDRESS if started_at == A_BROWSERS_ADDRESS else A_BROWSERS_ADDRESS
    browser = hosts.browser(started_at)
    query = hosts.issuer.authorize(
        browser.get(f"{started_at}/login/oidc").headers["location"],
        subject="subject-1",
        groups=MEMBER,
        username=A.username,
    )
    carried = hosts.browser_with_the_cookies_of(browser, other)

    answer = carried.get(f"{other}/auth/callback?{query}")

    assert hosts.session_count() == 0
    if other == A_TALK_ADDRESS:
        assert answer.status_code == HTTPStatus.SEE_OTHER
        assert answer.headers["location"] == (
            f"{A_BROWSERS_ADDRESS}/login/talk/login-failed"
        )
    else:
        assert answer.status_code == HTTPStatus.FORBIDDEN
        assert f"<h1>{ENGLISH.login_failed_title}</h1>" in answer.text


# T7c.
@pytest.mark.parametrize(
    "lobby_first", [True, False], ids=["lobby first", "talk first"]
)
def test_two_sign_ins_in_one_browser_complete_each_on_its_own_host(
    hosts: Hosts,
    lobby_first: bool,  # noqa: FBT001
) -> None:
    browser = hosts.browser(A_BROWSERS_ADDRESS)
    queries: dict[str, str] = {}
    for address in (A_BROWSERS_ADDRESS, A_TALK_ADDRESS):
        queries[address] = hosts.issuer.authorize(
            browser.get(f"{address}/login/oidc").headers["location"],
            subject="subject-1",
            groups=MEMBER,
            username=A.username,
        )
    order = (
        [A_BROWSERS_ADDRESS, A_TALK_ADDRESS]
        if lobby_first
        else [A_TALK_ADDRESS, A_BROWSERS_ADDRESS]
    )

    answers = [
        browser.get(f"{address}/auth/callback?{queries[address]}") for address in order
    ]

    assert [answer.status_code for answer in answers] == [HTTPStatus.SEE_OTHER] * 2
    hosts.give_a_deck_to(A.username)
    assert (
        browser.get(f"{A_TALK_ADDRESS}/deck/{A_DECK}/presenter/").status_code
        == HTTPStatus.OK
    )
    assert browser.get(f"{A_BROWSERS_ADDRESS}/").status_code == HTTPStatus.OK


# ---- a refused sign-in at the talk host --------------------------------------


def _refused(tmp_path: Path, reason: str) -> tuple[Hosts, TestClient, Response]:
    """A talk sign-in that fails for this reason, and the answer it got."""
    foreign = AdmissionConfig(
        bound_issuer="https://another-issuer.example.test/realms/test",
        login_group=MEMBER[0],
        admin_group=f"{MEMBER[0]}/admin",
    )
    hosts = two_hosts(tmp_path, admission=foreign if reason == "foreign" else None)
    browser = hosts.browser(A_TALK_ADDRESS)
    groups = [] if reason == "not a member" else MEMBER
    if reason == "no issuer":
        hosts.issuer.token_endpoint_is_down = True
    if reason == "username taken":
        seed_user(hosts.database, A)
    return hosts, browser, hosts.sign_in(browser, A_TALK_ADDRESS, groups=groups)


_REFUSALS = [
    ("not a member", "no-access"),
    ("no issuer", "login-failed"),
    ("foreign", "login-failed"),
    ("username taken", "login-failed"),
]


# T9a.
@pytest.mark.parametrize(("reason", "state"), _REFUSALS)
def test_a_refused_talk_sign_in_goes_to_the_lobbys_refusal_route_with_no_session(
    tmp_path: Path, reason: str, state: str
) -> None:
    hosts, _browser, answer = _refused(tmp_path, reason)

    cookies = answer.headers.get_list("set-cookie")
    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert answer.headers["location"] == f"{A_BROWSERS_ADDRESS}/login/talk/{state}"
    assert not any(c.startswith(f"{DEFAULT_SESSION_COOKIE_NAME}=") for c in cookies)
    assert hosts.session_count() == 0


# T9a.
@pytest.mark.parametrize("reason", [reason for reason, _state in _REFUSALS])
def test_a_refused_talk_sign_in_clears_the_talk_hosts_pending_login(
    tmp_path: Path, reason: str
) -> None:
    _hosts, _browser, answer = _refused(tmp_path, reason)

    pending = [c for c in answer.headers.get_list("set-cookie") if "oidc_pending" in c]
    assert len(pending) == 1
    assert "max-age=0" in pending[0].lower()


# T9c.
def test_trying_again_after_a_refusal_signs_in_and_the_talk_host_sends_on_to_the_lobby(
    tmp_path: Path,
) -> None:
    hosts, browser, _refusal = _refused(tmp_path, "not a member")

    retried = hosts.sign_in(browser, A_TALK_ADDRESS)
    landed = browser.get(retried.headers["location"])

    assert retried.status_code == HTTPStatus.SEE_OTHER
    assert hosts.session_count() == 1
    assert landed.headers["location"] == A_BROWSERS_ADDRESS


# ---- ending sessions ---------------------------------------------------------


# T10.
def test_a_verified_logout_token_at_the_lobby_ends_the_talk_session(
    hosts: Hosts,
) -> None:
    talk = hosts.browser(A_TALK_ADDRESS)
    hosts.sign_in(talk, A_TALK_ADDRESS)
    hosts.give_a_deck_to(A.username)
    presenter = f"/deck/{A_DECK}/presenter/"
    assert talk.get(presenter).status_code == HTTPStatus.OK

    token = hosts.issuer.mint_logout_token(subject="subject-1")
    ended = hosts.browser(A_BROWSERS_ADDRESS).post(
        "/auth/backchannel-logout",
        content=urlencode({"logout_token": token}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert ended.status_code == HTTPStatus.OK
    assert talk.get(presenter).status_code == HTTPStatus.FOUND


# T11.
def test_a_lobby_logout_leaves_the_talk_session_standing(hosts: Hosts) -> None:
    browser = hosts.browser(A_BROWSERS_ADDRESS)
    hosts.sign_in(browser, A_BROWSERS_ADDRESS)
    hosts.sign_in(browser, A_TALK_ADDRESS)
    hosts.give_a_deck_to(A.username)

    ended = browser.post(f"{A_BROWSERS_ADDRESS}/logout", headers=_LOBBY_ORIGIN_CLAIMS)

    assert ended.status_code == HTTPStatus.SEE_OTHER
    assert browser.get(f"{A_BROWSERS_ADDRESS}/").status_code == HTTPStatus.FOUND
    assert (
        browser.get(f"{A_TALK_ADDRESS}/deck/{A_DECK}/presenter/").status_code
        == HTTPStatus.OK
    )


# ---- serving the owner's talk ------------------------------------------------

_INDEX = (_BUILT_TALK / "index.html").read_bytes()
_ASSET = "assets/index-mRcVgIkQ.js"


def _six_requests(talk: TestClient, slug: str) -> list[Response]:
    base = f"/deck/{slug}"
    asset = talk.get(f"{base}/{_ASSET}")
    validator = asset.headers.get("etag")
    return [
        talk.get(f"{base}/presenter/"),
        talk.get(f"{base}/{_ASSET}"),
        talk.get(f"{base}/no/such/page/"),
        talk.head(f"{base}/{_ASSET}"),
        talk.get(f"{base}/{_ASSET}", headers={"range": "bytes=0-9"}),
        talk.get(
            f"{base}/{_ASSET}",
            headers={"if-none-match": validator or '"unknown"'},
        ),
    ]


# T12a.
def test_the_owner_gets_the_talk_its_assets_ranges_and_validators(
    hosts: Hosts,
) -> None:
    presenter, asset, fallback, head, ranged, conditional = _six_requests(
        hosts.signed_in_at(A_TALK_ADDRESS), A_DECK
    )

    assert presenter.content == _INDEX
    assert asset.content == (_BUILT_TALK / _ASSET).read_bytes()
    assert fallback.content == _INDEX
    assert (head.status_code, head.content) == (HTTPStatus.OK, b"")
    assert ranged.status_code == HTTPStatus.PARTIAL_CONTENT
    assert conditional.status_code == HTTPStatus.NOT_MODIFIED


# T12b.
@pytest.mark.parametrize("slug", [B_DECK, NO_DECK])
def test_a_deck_of_someone_else_and_a_deck_that_is_nobodys_answer_alike(
    hosts: Hosts, slug: str
) -> None:
    answers = _six_requests(hosts.signed_in_at(A_TALK_ADDRESS), slug)

    for answer in answers:
        assert answer.status_code == HTTPStatus.NOT_FOUND
        assert answer.content == b""
        assert "etag" not in answer.headers
        assert "last-modified" not in answer.headers


# T13.
@pytest.mark.parametrize(
    "arrange",
    ["gate", "not found", "refusal", "talk"],
)
def test_every_answer_of_the_talk_host_carries_the_talk_policy_headers(
    hosts: Hosts, arrange: str
) -> None:
    signed_in = hosts.signed_in_at(A_TALK_ADDRESS)
    answer = {
        "gate": lambda: hosts.browser(A_TALK_ADDRESS).get(f"/deck/{A_DECK}/"),
        "not found": lambda: signed_in.get("/settings"),
        "refusal": lambda: hosts.browser(A_TALK_ADDRESS).get("/auth/callback"),
        "talk": lambda: signed_in.get(f"/deck/{A_DECK}/presenter/"),
    }[arrange]()

    assert answer.headers["content-security-policy"] == talk_content_security_policy()
    assert "frame-ancestors 'none'" in answer.headers["content-security-policy"]
    assert answer.headers["permissions-policy"] == TALK_PERMISSIONS_POLICY
    assert answer.headers["cache-control"] == "no-store"
    assert "max-age" in answer.headers["strict-transport-security"]


# ---- each host refuses the other's origin ------------------------------------

_LOBBY_ORIGIN_CLAIMS = {"origin": A_BROWSERS_ADDRESS, "sec-fetch-site": "same-site"}
_FROM_THE_LOBBY = [
    pytest.param(_LOBBY_ORIGIN_CLAIMS, id="origin and same-site"),
    pytest.param({"referer": f"{A_BROWSERS_ADDRESS}/page"}, id="referer only"),
    pytest.param({"origin": "https://evil.example.test"}, id="foreign origin"),
]


# T14a.
@pytest.mark.parametrize("claims", _FROM_THE_LOBBY)
def test_the_talk_host_refuses_a_form_that_the_lobby_or_a_stranger_submitted(
    hosts: Hosts, claims: dict[str, str]
) -> None:
    talk = hosts.signed_in_at(A_TALK_ADDRESS)

    answer = talk.post(f"/deck/{A_DECK}/presenter/", headers=claims)

    assert answer.status_code == HTTPStatus.FORBIDDEN


# T14a.
def test_the_talk_host_passes_its_own_origin_to_the_talk_files(hosts: Hosts) -> None:
    talk = hosts.signed_in_at(A_TALK_ADDRESS)

    answer = talk.post(f"/deck/{A_DECK}/presenter/", headers=_TALK_ORIGIN_CLAIMS)

    assert answer.status_code == HTTPStatus.METHOD_NOT_ALLOWED


# T14b.
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(
            {"origin": A_TALK_ADDRESS, "sec-fetch-site": "same-site"},
            id="origin and same-site",
        ),
        pytest.param({"referer": f"{A_TALK_ADDRESS}/deck"}, id="referer only"),
    ],
)
@pytest.mark.parametrize("path", ["/logout", "/settings/sources/new"])
def test_the_lobby_refuses_a_form_that_the_talk_host_submitted(
    hosts: Hosts, claims: dict[str, str], path: str
) -> None:
    lobby = hosts.signed_in_at(A_BROWSERS_ADDRESS)

    answer = lobby.post(path, headers=claims)

    assert answer.status_code == HTTPStatus.FORBIDDEN
