"""A member signs in through the issuer's return; every refusal shows its page.

The issuer is a fake that signs with a key the test generated; the accounts and
the sessions are the real SQLite stores on a temporary file.
"""

import base64
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI, Response
from httpx2 import Response as BrowserAnswer
from pydantic import SecretStr
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME, DEFAULT_SESSION_COOKIE_NAME
from webauth.oidc_login import AdmissionConfig
from webauth.ports import (
    AccountEvent,
    AccountEventKind,
    AuditSink,
    SubjectUserStore,
    UserRecord,
)

from presentator.adapters.audit import LoggingAuditSink
from presentator.adapters.identity import (
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    StoredSession,
)
from presentator.api.auth import LOGOUT_PATH, carry_set_cookies
from presentator.application.identity import SESSION_LIFETIME
from presentator.host import main
from tests.api.lobby import (
    AN_ISSUER,
    a_settings,
)
from tests.sign_in_support import (
    ADMIN,
    MEMBER,
    Scene,
    a_scene,
    cookie_names,
    on_an_event_loop,
)

_BOUND = 30.0
_TOO_LONG_FOR_A_COOKIE = 4000
_COOKIES_OF_A_SIGN_IN = 3
_HOLDS_OF_A_SIGN_IN = 2


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
            self.held_on_the_event_loop |= on_an_event_loop()
        with ProcessWriteLock(primitive=self.primitive).hold():
            yield


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


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return a_scene(tmp_path)


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
    (pending,) = (
        cookie
        for cookie in answer.headers.get_list("set-cookie")
        if "Max-Age=0" in cookie
    )
    assert pending.startswith("__Host-")
    assert len(cookie_names(answer)) == _COOKIES_OF_A_SIGN_IN
    assert not [name for name in cookie_names(answer) if "id_token" in name]


def _claims_of(token: str | None) -> dict[str, object]:
    assert token is not None
    payload = token.split(".")[1]
    document: dict[str, object] = json.loads(base64.urlsafe_b64decode(payload + "=="))
    return document


def test_the_session_row_holds_the_id_token_the_sign_in_returned(
    scene: Scene,
) -> None:
    """#428 cell 9: the row holds the ID token the issuer returned."""
    scene.sign_in(username="felix")

    (stored,) = scene.stored_id_tokens()
    claims = _claims_of(stored)
    assert (claims["sub"], claims["preferred_username"]) == ("subject-1", "felix")


def test_an_id_token_longer_than_a_cookie_could_hold_is_stored_whole(
    scene: Scene,
) -> None:
    """#428 cell 10: an ID token past the old cookie bound is stored whole."""
    answer = scene.sign_in(username="x" * _TOO_LONG_FOR_A_COOKIE)

    assert answer.status_code == HTTPStatus.SEE_OTHER
    (stored,) = scene.stored_id_tokens()
    assert _claims_of(stored)["preferred_username"] == "x" * _TOO_LONG_FOR_A_COOKIE


@pytest.mark.parametrize(
    ("groups", "role"),
    [
        pytest.param(MEMBER, "user", id="member"),
        pytest.param(ADMIN, "admin", id="admin group member"),
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
    scene.sign_in(groups=ADMIN)
    scene.sign_in(groups=ADMIN)
    scene.sign_in(groups=MEMBER)

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
        authorization, subject="subject-1", groups=MEMBER, username="felix"
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
        authorization, subject="subject-1", groups=MEMBER, username="felix"
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


def test_log_out_on_the_no_access_page_asks_the_issuer_without_a_hint(
    scene: Scene,
) -> None:
    """#428 cell 13: the no-access page logs out without a hint."""
    _in_another_group(scene)

    answer = scene.lobby.client.post(
        LOGOUT_PATH, headers={"content-type": "application/x-www-form-urlencoded"}
    )

    assert answer.status_code == HTTPStatus.OK
    assert 'name="post_logout_redirect_uri"' in answer.text
    assert "id_token_hint" not in answer.text
    assert scene.count("sessions") == 0
    for name in (DEFAULT_SESSION_COOKIE_NAME, DEFAULT_CSRF_COOKIE_NAME):
        assert "Max-Age=0" in _cookie(answer, name)


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

    def create_unless_the_disk_fails(  # noqa: PLR0913
        store: SqliteSessionRecordStore,
        user_id: str,
        *,
        now: datetime,
        expires_at: datetime,
        refresh_token: SecretStr | None,
        id_token: SecretStr,
    ) -> StoredSession:
        if failures:
            raise failures.pop()
        return create(
            store,
            user_id,
            now=now,
            expires_at=expires_at,
            refresh_token=refresh_token,
            id_token=id_token,
        )

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
