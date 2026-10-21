"""A member signs in through the issuer's return; every refusal shows its page.

The issuer is a fake that signs with a key the test generated; the accounts and
the sessions are the real SQLite stores on a temporary file.
"""

import asyncio
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from httpx2 import Response as BrowserAnswer
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME, DEFAULT_SESSION_COOKIE_NAME
from webauth.oidc_login import AdmissionConfig
from webauth.ports import (
    AccountEvent,
    AccountEventKind,
    AuditSink,
    SubjectUserStore,
    UserRecord,
    WriteLock,
)

from presentator.adapters.audit import LoggingAuditSink
from presentator.adapters.identity import (
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    create_identity_tables,
)
from presentator.api.auth import LOGOUT_PATH, carry_set_cookies
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Session
from presentator.host import main
from tests.api.fake_issuer import FakeIssuer
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    A_CLIENT_ID,
    AN_ISSUER,
    Lobby,
    SigningIn,
    a_browser_at,
    a_crowd,
    a_lobby_app,
    a_relying_party,
    a_settings,
    an_installed_auth,
)

_MEMBER = ["/apps/presentator"]
_ADMIN = ["/apps/presentator", "/apps/presentator/admin"]
_BOUND = 30.0
_TOO_LONG_FOR_A_COOKIE = 4000
_COOKIES_OF_A_SIGN_IN = 4
_HOLDS_OF_A_SIGN_IN = 2
_LOGOUT_HINT_NAME = "id_token_hint"
_COUNTS = {
    "users": "SELECT count(*) FROM users",
    "sessions": "SELECT count(*) FROM sessions",
}


@dataclass
class RecordingAudit:
    """The audit lines a sign-in wrote."""

    events: list[AccountEvent] = field(default_factory=list[AccountEvent])

    def account_event(self, event: AccountEvent) -> None:
        self.events.append(event)


@dataclass
class ContentionSignalingLock:
    """A real re-entrant lock that says when a taker found it held."""

    contended: threading.Event = field(default_factory=threading.Event)
    _real: threading.RLock = field(default_factory=threading.RLock)

    def __enter__(self) -> bool:
        """Take the lock, announcing first when it was already held."""
        if self._real.acquire(blocking=False):
            return True
        self.contended.set()
        return self._real.acquire()

    def __exit__(self, *_exception: object) -> None:
        """Let go of the lock."""
        self._real.release()


@dataclass
class WatchedLock:
    """The process lock over a contention-signaling primitive, counting its holds."""

    primitive: ContentionSignalingLock = field(default_factory=ContentionSignalingLock)
    holds: int = 0
    held_on_the_event_loop: bool = False
    _mutex: threading.Lock = field(default_factory=threading.Lock)

    @property
    def contended(self) -> threading.Event:
        return self.primitive.contended

    @contextmanager
    def hold(self) -> Generator[None]:
        with self._mutex:
            self.holds += 1
            self.held_on_the_event_loop |= _on_an_event_loop()
        with ProcessWriteLock(primitive=self.primitive).hold():
            yield


def _on_an_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@dataclass
class GatedAccounts:
    """Accounts whose first read waits for the test, after reading."""

    accounts: SqliteSubjectUserStore
    reads: int = 0
    first_read: threading.Event = field(default_factory=threading.Event)
    release_first: threading.Event = field(default_factory=threading.Event)

    def get(self, user_id: str) -> UserRecord | None:
        found = self.accounts.get(user_id)
        self.reads += 1
        if self.reads == 1:
            self.first_read.set()
            assert self.release_first.wait(_BOUND)
        return found

    def create_for_subject(self, subject: str, username: str, role: str) -> UserRecord:
        return self.accounts.create_for_subject(subject, username, role)

    def set_role(self, user_id: str, role: str) -> UserRecord:
        return self.accounts.set_role(user_id, role)


@dataclass(frozen=True, slots=True, kw_only=True)
class Scene:
    """A lobby that signs people in through a fake issuer, over real stores."""

    lobby: Lobby
    issuer: FakeIssuer
    database: Path
    users: SqliteSubjectUserStore
    audit: RecordingAudit

    def browser(self, *, raise_server_exceptions: bool = True) -> TestClient:
        return TestClient(
            self.lobby.app,
            base_url=A_BROWSERS_ADDRESS,
            follow_redirects=False,
            headers=A_BROWSERS_HEADERS,
            raise_server_exceptions=raise_server_exceptions,
        )

    def begin(self, browser: TestClient, next_path: str | None = None) -> str:
        """Start the sign-in and return the issuer's authorization address."""
        params = {} if next_path is None else {"next": next_path}
        return browser.get("/login/oidc", params=params).headers["location"]

    def sign_in(
        self,
        browser: TestClient | None = None,
        *,
        subject: str = "subject-1",
        groups: list[str] | None = None,
        username: str | None = "felix",
        next_path: str | None = None,
    ) -> BrowserAnswer:
        """A person signs in at the issuer and the browser comes back."""
        client = browser or self.lobby.client
        query = self.issuer.authorize(
            self.begin(client, next_path),
            subject=subject,
            groups=_MEMBER if groups is None else groups,
            username=username,
        )
        return client.get(f"/auth/callback?{query}")

    def count(self, table: Literal["users", "sessions"]) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            (count,) = connection.execute(_COUNTS[table]).fetchone()
        return int(count)


def a_scene(
    tmp_path: Path,
    *,
    accounts: Callable[[SqliteSubjectUserStore], SubjectUserStore] = lambda real: real,
    lock: WriteLock | None = None,
    admission: AdmissionConfig | None = None,
) -> Scene:
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    people = a_crowd()
    issuer = FakeIssuer(issuer=AN_ISSUER, client_id=A_CLIENT_ID, now=people.clock.now())
    users = SqliteSubjectUserStore(database)
    audit = RecordingAudit()
    auth = an_installed_auth(
        people.clock,
        relying_party=a_relying_party(
            people.clock,
            post_token_request=issuer.post_token_request,
            fetch_key_set=issuer.fetch_key_set,
        ),
        accounts=accounts(users),
        lock=lock,
        audit=audit,
    )
    app, _ = a_lobby_app(
        people=people,
        signing_in=SigningIn(
            sessions=SqliteSessionRecordStore(database),
            auth=auth if admission is None else replace(auth, admission=admission),
        ),
    )
    lobby = a_browser_at(
        app, people=people, base_url=A_BROWSERS_ADDRESS, headers=A_BROWSERS_HEADERS
    )
    return Scene(
        lobby=lobby, issuer=issuer, database=database, users=users, audit=audit
    )


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return a_scene(tmp_path)


def _cookie_names(answer: BrowserAnswer) -> list[str]:
    return [cookie.split("=", 1)[0] for cookie in answer.headers.get_list("set-cookie")]


def _cookie(answer: BrowserAnswer, name_part: str) -> str:
    (cookie,) = (
        cookie
        for cookie in answer.headers.get_list("set-cookie")
        if name_part in cookie.split("=", 1)[0]
    )
    return cookie


def test_a_member_signs_in_and_the_303_carries_every_cookie_the_sign_in_needs(
    scene: Scene,
) -> None:
    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert answer.headers["location"] == "/"
    lifetime = f"Max-Age={int(SESSION_LIFETIME.total_seconds())}"
    for name in (DEFAULT_SESSION_COOKIE_NAME, DEFAULT_CSRF_COOKIE_NAME):
        assert lifetime in _cookie(answer, name)
        assert "Secure" in _cookie(answer, name)
    session = _cookie(answer, DEFAULT_SESSION_COOKIE_NAME)
    assert "HttpOnly" in session
    assert "SameSite=lax" in session
    assert "HttpOnly" not in _cookie(answer, DEFAULT_CSRF_COOKIE_NAME)
    hint = _cookie(answer, _LOGOUT_HINT_NAME)
    assert f"Path={LOGOUT_PATH}" in hint
    assert lifetime in hint
    assert "Secure" in hint
    assert "HttpOnly" in hint
    (pending,) = (
        cookie
        for cookie in answer.headers.get_list("set-cookie")
        if "Max-Age=0" in cookie and _LOGOUT_HINT_NAME not in cookie
    )
    assert pending.startswith("__Host-")
    assert len(_cookie_names(answer)) == _COOKIES_OF_A_SIGN_IN


def test_a_token_too_long_for_a_cookie_clears_the_hint_the_way_the_library_does(
    scene: Scene,
) -> None:
    answer = scene.sign_in(username="x" * _TOO_LONG_FOR_A_COOKIE)

    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert "Max-Age=0" in _cookie(answer, _LOGOUT_HINT_NAME)


@pytest.mark.parametrize(
    ("groups", "role"),
    [
        pytest.param(_MEMBER, "user", id="member"),
        pytest.param(_ADMIN, "admin", id="admin group member"),
        pytest.param(["/apps/presentator/admin"], "admin", id="admin group alone"),
    ],
)
def test_the_groups_the_issuer_names_decide_the_role_the_account_gets(
    scene: Scene, groups: list[str], role: str
) -> None:
    scene.sign_in(groups=groups)

    account = scene.users.get("subject-1")
    assert account is not None
    assert account.role == role


def test_a_changed_membership_changes_the_role_at_the_next_sign_in_and_is_audited(
    scene: Scene,
) -> None:
    scene.sign_in(groups=_ADMIN)
    scene.sign_in(groups=_ADMIN)
    scene.sign_in(groups=_MEMBER)

    account = scene.users.get("subject-1")
    assert account is not None
    assert account.role == "user"
    assert [
        (event.kind, event.role, event.previous_role) for event in scene.audit.events
    ] == [
        (AccountEventKind.USER_CREATED, "admin", None),
        (AccountEventKind.ROLE_CHANGED, "user", "admin"),
    ]


def test_the_request_after_sign_in_is_signed_in_through_the_session_decider(
    scene: Scene,
) -> None:
    scene.sign_in(username="anna")

    opened = scene.lobby.client.get("/")

    assert opened.status_code == HTTPStatus.OK
    assert "anna" in opened.text
    assert scene.count("sessions") == 1


def test_an_ordinary_answer_refreshes_no_cookie_and_a_later_sign_in_issues_fresh_ones(
    scene: Scene,
) -> None:
    first = scene.sign_in()

    assert scene.lobby.client.get("/").headers.get_list("set-cookie") == []
    scene.lobby.client.post(LOGOUT_PATH)
    second = scene.sign_in()

    for name in (DEFAULT_SESSION_COOKIE_NAME, DEFAULT_CSRF_COOKIE_NAME):
        assert _cookie(second, name) != _cookie(first, name)


@pytest.mark.parametrize(
    ("next_path", "landing"),
    [
        pytest.param("//evil.example", "/", id="scheme-relative"),
        pytest.param("https://evil.example/", "/", id="absolute"),
        pytest.param("/\\evil", "/", id="backslash"),
        pytest.param("/decks/x?y=1", "/decks/x?y=1", id="a path of the app"),
    ],
)
def test_the_sign_in_lands_on_a_path_of_the_app_and_on_nothing_foreign(
    scene: Scene, next_path: str, landing: str
) -> None:
    answer = scene.sign_in(next_path=next_path)

    assert answer.headers["location"] == landing


def _state_mismatch(scene: Scene) -> BrowserAnswer:
    scene.begin(scene.lobby.client)
    return scene.lobby.client.get("/auth/callback?code=a-code&state=not-the-state")


def _expired_pending_login(scene: Scene) -> BrowserAnswer:
    authorization = scene.begin(scene.lobby.client)
    query = scene.issuer.authorize(
        authorization, subject="subject-1", groups=_MEMBER, username="felix"
    )
    scene.lobby.clock.advance(timedelta(hours=1))
    return scene.lobby.client.get(f"/auth/callback?{query}")


def _token_endpoint_down(scene: Scene) -> BrowserAnswer:
    scene.issuer.token_endpoint_is_down = True
    return scene.sign_in()


def _key_set_down(scene: Scene) -> BrowserAnswer:
    scene.issuer.key_set_is_down = True
    return scene.sign_in()


def _the_person_declined(scene: Scene) -> BrowserAnswer:
    authorization = scene.begin(scene.lobby.client)
    state = scene.issuer.authorize(
        authorization, subject="subject-1", groups=_MEMBER, username="felix"
    ).split("state=")[1]
    return scene.lobby.client.get(f"/auth/callback?error=access_denied&state={state}")


def _in_another_group(scene: Scene) -> BrowserAnswer:
    return scene.sign_in(groups=["/apps/another"])


def _in_no_group(scene: Scene) -> BrowserAnswer:
    return scene.sign_in(groups=[])


def _in_groups_named_without_a_path(scene: Scene) -> BrowserAnswer:
    return scene.sign_in(groups=["presentator", "admin"])


def _a_username_another_subject_holds(scene: Scene) -> BrowserAnswer:
    scene.users.create_for_subject("subject-0", "felix", "user")
    return scene.sign_in(subject="subject-1", username="felix")


_REFUSALS = [
    pytest.param(_state_mismatch, id="state mismatch"),
    pytest.param(_expired_pending_login, id="expired pending login"),
    pytest.param(_token_endpoint_down, id="token endpoint down"),
    pytest.param(_key_set_down, id="key set down"),
    pytest.param(_the_person_declined, id="the person declined"),
    pytest.param(_a_username_another_subject_holds, id="username taken"),
]
_NO_ACCESS = [
    pytest.param(_in_another_group, id="another group"),
    pytest.param(_in_no_group, id="no group"),
    pytest.param(_in_groups_named_without_a_path, id="bare group names"),
]
_ALL_REFUSALS = [*_REFUSALS, *_NO_ACCESS]
_LEGAL_LINKS = ('href="https://hallucinai.de/impressum"', 'href="/datenschutz"')


@pytest.mark.parametrize("drive", _REFUSALS)
def test_every_refusal_shows_the_one_login_failed_page_without_an_identity(
    scene: Scene, drive: Callable[[Scene], BrowserAnswer]
) -> None:
    page = drive(scene)

    assert page.status_code == HTTPStatus.FORBIDDEN
    assert "<h1>Login failed</h1>" in page.text
    assert "Sign-in did not work. Please try again." in page.text
    assert (
        page.text.count('data-action data-wide href="/login/oidc">Try again</a>') == 1
    )
    assert "No access" not in page.text
    assert all(link in page.text for link in _LEGAL_LINKS)
    assert "felix" not in page.text
    assert int(page.headers["content-length"]) == len(page.content)


@pytest.mark.parametrize("drive", _NO_ACCESS)
def test_a_person_outside_the_group_gets_the_no_access_page_without_an_identity(
    scene: Scene, drive: Callable[[Scene], BrowserAnswer]
) -> None:
    page = drive(scene)

    assert page.status_code == HTTPStatus.FORBIDDEN
    assert "<h1>No access</h1>" in page.text
    assert (
        "You are signed in, but this account has no access to presentator." in page.text
    )
    assert "Login failed" not in page.text
    assert all(link in page.text for link in _LEGAL_LINKS)
    for identity in ("felix", "subject-1", "/apps/another"):
        assert identity not in page.text
    assert int(page.headers["content-length"]) == len(page.content)


def test_the_no_access_page_offers_log_out_and_a_back_that_needs_no_script(
    scene: Scene,
) -> None:
    page = _in_another_group(scene).text

    assert page.count('<form method="post" action="/logout">') == 1
    assert page.count('type="submit"') == 1
    assert "Log out" in page
    assert 'data-action data-quiet data-history-back href="/login"' in page
    assert '<script src="/static/history-back.js" defer></script>' in page
    assert re.search(r"\son[a-z]+=", page) is None
    assert page.count("<script") == page.count('<script src="')


def test_the_back_script_is_served_without_a_session(scene: Scene) -> None:
    script = scene.lobby.client.get("/static/history-back.js")

    assert script.status_code == HTTPStatus.OK
    assert "history.back()" in script.text


def test_log_out_on_the_no_access_page_leaves_for_the_issuer_without_a_hint(
    scene: Scene,
) -> None:
    _in_another_group(scene)

    answer = scene.lobby.client.post(
        LOGOUT_PATH, headers={"content-type": "application/x-www-form-urlencoded"}
    )

    assert answer.status_code == HTTPStatus.SEE_OTHER
    query = parse_qs(urlsplit(answer.headers["location"]).query)
    assert "post_logout_redirect_uri" in query
    assert "id_token_hint" not in query
    assert scene.count("sessions") == 0
    for name in (
        DEFAULT_SESSION_COOKIE_NAME,
        DEFAULT_CSRF_COOKIE_NAME,
        _LOGOUT_HINT_NAME,
    ):
        assert "Max-Age=0" in _cookie(answer, name)
    assert len(_cookie_names(answer)) == 3


def test_a_member_who_left_the_group_is_refused_and_returns_to_the_same_account(
    scene: Scene,
) -> None:
    scene.sign_in()
    account = scene.users.get("subject-1")

    refused = scene.sign_in(groups=["/apps/another"])

    assert "<h1>No access</h1>" in refused.text
    assert (scene.count("users"), scene.count("sessions")) == (1, 1)
    assert [event.kind for event in scene.audit.events] == [
        AccountEventKind.USER_CREATED
    ]
    assert scene.sign_in().status_code == HTTPStatus.SEE_OTHER
    assert scene.count("users") == 1
    assert scene.users.get("subject-1") == account


@pytest.mark.parametrize("drive", _ALL_REFUSALS)
def test_a_refusal_sets_no_session_cookie_and_keeps_only_what_the_library_cleared(
    scene: Scene, drive: Callable[[Scene], BrowserAnswer]
) -> None:
    page = drive(scene)

    (cleared,) = page.headers.get_list("set-cookie")
    assert cleared.startswith("__Host-")
    assert "Max-Age=0" in cleared
    assert _LOGOUT_HINT_NAME not in cleared


@pytest.mark.parametrize("drive", _ALL_REFUSALS)
def test_a_refusal_writes_no_session_and_no_account(
    scene: Scene, drive: Callable[[Scene], BrowserAnswer]
) -> None:
    accounts_before = scene.count("users")

    drive(scene)

    assert scene.count("sessions") == 0
    assert scene.count("users") == accounts_before + (
        1 if drive is _a_username_another_subject_holds else 0
    )


def test_an_issuer_that_differs_in_case_from_the_bound_one_is_a_foreign_issuer(
    tmp_path: Path,
) -> None:
    admission = AdmissionConfig(
        bound_issuer=AN_ISSUER.replace("auth.", "AUTH."),
        login_group="/apps/presentator",
        admin_group="/apps/presentator/admin",
    )
    scene = a_scene(tmp_path, admission=admission)

    page = scene.sign_in()

    assert page.status_code == HTTPStatus.FORBIDDEN
    assert "<h1>Login failed</h1>" in page.text
    assert (scene.count("users"), scene.count("sessions")) == (0, 0)


def test_the_cookies_of_the_library_are_copied_whole_and_nothing_else() -> None:
    kept = Response()
    kept.set_cookie("first", "1")
    kept.set_cookie("second", "2")
    page = Response(content="a page", status_code=HTTPStatus.FORBIDDEN)

    carry_set_cookies(kept, page)

    assert page.headers.getlist("set-cookie") == kept.headers.getlist("set-cookie")
    assert page.headers["content-length"] == str(len("a page"))


def test_a_signed_in_person_stays_signed_in_while_the_issuer_is_down(
    scene: Scene,
) -> None:
    scene.sign_in()
    scene.issuer.token_endpoint_is_down = True
    scene.issuer.key_set_is_down = True

    assert scene.lobby.client.get("/").status_code == HTTPStatus.OK


def test_two_first_sign_ins_of_one_subject_make_one_account_and_two_sessions(
    tmp_path: Path,
) -> None:
    gate: list[GatedAccounts] = []

    def gated(real: SqliteSubjectUserStore) -> SubjectUserStore:
        gate.append(GatedAccounts(real))
        return gate[0]

    lock = WatchedLock()
    scene = a_scene(tmp_path, accounts=gated, lock=lock)
    (accounts,) = gate

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(scene.sign_in, scene.browser())
        assert accounts.first_read.wait(_BOUND)
        second = pool.submit(scene.sign_in, scene.browser())
        assert lock.contended.wait(_BOUND)
        accounts.release_first.set()
        answers = [first.result(timeout=_BOUND), second.result(timeout=_BOUND)]

    assert [answer.status_code for answer in answers] == [HTTPStatus.SEE_OTHER] * 2
    assert (scene.count("users"), scene.count("sessions")) == (1, 2)
    assert [event.kind for event in scene.audit.events] == [
        AccountEventKind.USER_CREATED
    ]


def test_one_callback_holds_the_write_lock_once_to_admit_and_once_to_open_the_session(
    tmp_path: Path,
) -> None:
    lock = WatchedLock()
    scene = a_scene(tmp_path, lock=lock)

    scene.sign_in()

    assert lock.holds == _HOLDS_OF_A_SIGN_IN


def test_the_write_lock_is_held_on_worker_threads_and_never_on_the_event_loop(
    tmp_path: Path,
) -> None:
    lock = WatchedLock()
    scene = a_scene(tmp_path, lock=lock)

    scene.sign_in()

    assert lock.holds == _HOLDS_OF_A_SIGN_IN
    assert not lock.held_on_the_event_loop


def test_a_store_failure_after_the_admission_keeps_the_account_and_sets_no_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = a_scene(tmp_path)
    create = SqliteSessionRecordStore.create
    failures = [sqlite3.OperationalError("disk I/O error")]

    def create_unless_the_disk_fails(
        store: SqliteSessionRecordStore,
        user_id: str,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> Session:
        if failures:
            raise failures.pop()
        return create(store, user_id, now=now, expires_at=expires_at)

    monkeypatch.setattr(
        SqliteSessionRecordStore, "create", create_unless_the_disk_fails
    )
    browser = scene.browser(raise_server_exceptions=False)

    failed = scene.sign_in(browser)

    assert failed.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert failed.headers.get_list("set-cookie") == []
    assert (scene.count("users"), scene.count("sessions")) == (1, 0)
    retried = scene.sign_in(browser)
    assert retried.status_code == HTTPStatus.SEE_OTHER
    assert (scene.count("users"), scene.count("sessions")) == (1, 1)
    assert [event.kind for event in scene.audit.events] == [
        AccountEventKind.USER_CREATED
    ]


def test_the_audit_sink_logs_the_event_the_subject_and_the_roles_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink: AuditSink = LoggingAuditSink()

    with caplog.at_level(logging.INFO):
        sink.account_event(
            AccountEvent(
                kind=AccountEventKind.ROLE_CHANGED,
                actor_id=None,
                subject_id="subject-1",
                role="admin",
                previous_role="user",
            )
        )

    (record,) = caplog.records
    assert record.levelno == logging.INFO
    for fact in ("role_changed", "subject-1", "admin", "user"):
        assert fact in record.getMessage()


def test_an_audit_record_reaches_stderr_after_the_hosts_and_uvicorns_logging_setup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(main, "load_settings", a_settings)

    def an_instance_without_a_server(settings: object) -> SimpleNamespace:
        del settings
        return SimpleNamespace(lobby=FastAPI())

    monkeypatch.setattr(main, "build_instance", an_instance_without_a_server)

    async def configure_uvicorn(instance: SimpleNamespace, settings: object) -> None:
        del settings
        uvicorn.Config(instance.lobby)

    monkeypatch.setattr(main, "_serve", configure_uvicorn)

    main.main()
    LoggingAuditSink().account_event(
        AccountEvent(
            kind=AccountEventKind.USER_CREATED,
            actor_id=None,
            subject_id="subject-9",
            role="user",
        )
    )

    assert "user_created" in capsys.readouterr().err
