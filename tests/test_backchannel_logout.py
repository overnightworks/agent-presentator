"""Keycloak's back-channel logout ends every session of the person it names.

The issuer is the fake one, so every Logout Token is signed by a key the test
generated; the accounts and the sessions are the real SQLite stores.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response as Answer
from starlette.types import Message, Receive, Scope, Send
from webauth.ports import (
    AccountEvent,
    AccountEventKind,
    AuditSink,
    SubjectUserStore,
    UserRecord,
)

from presentator.adapters.identity import ProcessWriteLock, SqliteSubjectUserStore
from presentator.api.auth import (
    BACKCHANNEL_LOGOUT_PATH,
    PERMISSIONS_POLICY,
)
from tests.api.fake_issuer import FakeIssuer
from tests.api.lobby import A_BROWSERS_ADDRESS, a_content_security_policy
from tests.sign_in_support import RecordingAudit, Scene, a_scene

_FORM = "application/x-www-form-urlencoded"
_KEYCLOAK_BY_NAME = "http://presentator-lobby:8000"
_SIGNED_OUT_START = "/login/oidc?next=%2F"
_A_THIRD_OF_A_MEGABYTE = 33 * 1024
_THE_CAP = 32 * 1024
_BACKCHANNEL_ERROR = "presentator.api.auth"
_ROWS_OF_SUBJECT_1 = 2
_ALL_ROWS = 3


@dataclass
class RecordingLock:
    """The process lock, writing into a log when it is taken and when it is left."""

    log: list[object] = field(default_factory=list[object])
    sessions_seen: Callable[[], int] = lambda: 0
    real: ProcessWriteLock = field(default_factory=ProcessWriteLock)

    @contextmanager
    def hold(self) -> Generator[None]:
        self.log.append(("held", _on_an_event_loop()))
        with self.real.hold():
            yield
            self.log.append(("leaving", self.sessions_seen()))


def _on_an_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@dataclass
class Breakdown:
    """Whether the store behind a double is broken yet; the sign-ins come first."""

    now: bool = False


@dataclass(frozen=True)
class AccountsThatBreak:
    """The real accounts, whose read raises once the breakdown has begun."""

    real: SqliteSubjectUserStore
    breakdown: Breakdown

    def get(self, user_id: str) -> UserRecord | None:
        if self.breakdown.now:
            raise sqlite3.OperationalError(user_id)
        return self.real.get(user_id)

    def create_for_subject(self, subject: str, username: str, role: str) -> UserRecord:
        return self.real.create_for_subject(subject, username, role)

    def set_role(self, user_id: str, role: str) -> UserRecord:
        return self.real.set_role(user_id, role)


@dataclass(frozen=True)
class AuditThatBreaks:
    """The recording audit, whose sink raises once the breakdown has begun."""

    real: RecordingAudit
    breakdown: Breakdown

    def account_event(self, event: AccountEvent) -> None:
        if self.breakdown.now:
            raise OSError(event.kind)
        self.real.account_event(event)


@dataclass(frozen=True, slots=True, kw_only=True)
class World:
    """Subject-1 signed in in two browsers, subject-2 in a third."""

    scene: Scene
    lock: RecordingLock
    first: TestClient
    second: TestClient
    other: TestClient

    def keycloak(self, *, base_url: str = A_BROWSERS_ADDRESS) -> TestClient:
        """A server that sends no browser headers, no cookie and no Origin."""
        return TestClient(self.scene.lobby.app, base_url=base_url)

    def sessions_of(self, subject: str) -> int:
        with closing(sqlite3.connect(self.scene.database)) as connection:
            (count,) = connection.execute(
                "SELECT count(*) FROM sessions WHERE user_id = ?", (subject,)
            ).fetchone()
        return int(count)

    def token(self, subject: str = "subject-1", *, seconds_ago: int = 0) -> str:
        return self.scene.issuer.mint_logout_token(
            subject=subject, seconds_ago=seconds_ago
        )

    def logout_with(
        self, client: TestClient, token: str, *, path: str = BACKCHANNEL_LOGOUT_PATH
    ) -> Answer:
        return client.post(
            path,
            content=urlencode({"logout_token": token}),
            headers={"content-type": _FORM},
        )


def _arranged(
    tmp_path: Path,
    *,
    accounts: Callable[[SqliteSubjectUserStore], SubjectUserStore] = lambda real: real,
    audit: Callable[[RecordingAudit], AuditSink] = lambda recording: recording,
) -> World:
    lock = RecordingLock()
    scene = a_scene(tmp_path, lock=lock, accounts=accounts, audit=audit)
    lock.sessions_seen = lambda: scene.count("sessions")
    first, second, other = (scene.browser() for _ in range(3))
    scene.sign_in(first)
    scene.sign_in(second)
    scene.sign_in(other, subject="subject-2", username="anna")
    scene.audit.events.clear()
    lock.log.clear()
    return World(scene=scene, lock=lock, first=first, second=second, other=other)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return _arranged(tmp_path)


def _untouched(world: World) -> None:
    assert world.sessions_of("subject-1") == _ROWS_OF_SUBJECT_1
    assert world.sessions_of("subject-2") == 1
    assert world.scene.audit.events == []
    assert world.lock.log == []


def test_a_valid_token_ends_every_session_of_its_subject_and_no_other(
    world: World,
) -> None:
    answer = world.logout_with(world.keycloak(), world.token())

    assert answer.status_code == HTTPStatus.OK
    assert world.sessions_of("subject-1") == 0
    assert world.sessions_of("subject-2") == 1
    for ended in (world.first, world.second):
        reopened = ended.get("/")
        assert reopened.status_code == HTTPStatus.FOUND
        assert reopened.headers["location"] == _SIGNED_OUT_START
    assert world.other.get("/").status_code == HTTPStatus.OK


def test_the_answer_is_empty_and_never_cached(world: World) -> None:
    answer = world.logout_with(world.keycloak(), world.token())

    assert answer.content == b""
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["content-security-policy"] == a_content_security_policy()
    assert answer.headers["permissions-policy"] == PERMISSIONS_POLICY


def test_ending_sessions_writes_one_audit_event_naming_nobody_as_the_actor(
    world: World,
) -> None:
    world.logout_with(world.keycloak(), world.token())

    (event,) = world.scene.audit.events
    assert event.kind is AccountEventKind.BACKCHANNEL_LOGOUT
    assert event.actor_id is None
    assert event.subject_id == "subject-1"


def test_a_known_subject_with_no_open_session_changes_and_audits_nothing(
    world: World,
) -> None:
    world.scene.users.create_for_subject("subject-3", "carl", "user")
    users = world.scene.count("users")

    answer = world.logout_with(world.keycloak(), world.token("subject-3"))

    assert answer.status_code == HTTPStatus.OK
    assert world.scene.count("users") == users
    assert world.sessions_of("subject-1") + world.sessions_of("subject-2") == _ALL_ROWS
    assert world.scene.audit.events == []


def test_a_subject_with_no_account_is_accepted_and_creates_none(
    world: World,
) -> None:
    users = world.scene.count("users")

    answer = world.logout_with(world.keycloak(), world.token("a-stranger"))

    assert answer.status_code == HTTPStatus.OK
    assert world.scene.count("users") == users
    assert world.scene.count("sessions") == _ALL_ROWS
    assert world.scene.audit.events == []


@pytest.mark.parametrize(
    ("seconds_ago", "status", "sessions_left"),
    [
        pytest.param(149, HTTPStatus.OK, 0, id="inside-the-window-and-leeway"),
        pytest.param(151, HTTPStatus.BAD_REQUEST, _ROWS_OF_SUBJECT_1, id="past-it"),
    ],
)
def test_a_token_is_accepted_for_120_seconds_plus_the_30_second_leeway(
    world: World, seconds_ago: int, status: HTTPStatus, sessions_left: int
) -> None:
    answer = world.logout_with(world.keycloak(), world.token(seconds_ago=seconds_ago))

    assert answer.status_code == status
    assert world.sessions_of("subject-1") == sessions_left


_NOT_A_LOGOUT_TOKEN: dict[str, Callable[[FakeIssuer], str]] = {
    "the-id-token-of-the-sign-in": lambda issuer: issuer.mint(
        nonce="n", subject="subject-1", groups=[], username="felix"
    ),
    "a-nonce": lambda issuer: issuer.mint_logout_token(
        subject="subject-1", claims={"nonce": "n"}
    ),
    "a-foreign-audience": lambda issuer: issuer.mint_logout_token(
        subject="subject-1", claims={"aud": "another-client"}
    ),
    "another-key-under-the-same-kid": lambda issuer: issuer.mint_logout_token(
        subject="subject-1", by_another_key=True
    ),
    "a-session-and-no-subject": lambda issuer: issuer.mint_logout_token(
        subject="subject-1", claims={"sub": None, "sid": "a-session"}
    ),
    "no-events": lambda issuer: issuer.mint_logout_token(
        subject="subject-1", claims={"events": None}
    ),
}


@pytest.mark.parametrize("mint", _NOT_A_LOGOUT_TOKEN.values(), ids=_NOT_A_LOGOUT_TOKEN)
def test_a_token_that_is_not_a_logout_token_for_this_client_ends_nothing(
    world: World, mint: Callable[[FakeIssuer], str]
) -> None:
    answer = world.logout_with(world.keycloak(), mint(world.scene.issuer))

    assert answer.status_code == HTTPStatus.BAD_REQUEST
    _untouched(world)


_NOT_ONE_FORM: dict[str, Callable[[str], tuple[bytes, str]]] = {
    "an-empty-body": lambda _token: (b"", _FORM),
    "an-empty-token": lambda _token: (b"logout_token=", _FORM),
    "two-tokens": lambda token: (
        urlencode([("logout_token", token), ("logout_token", token)]).encode(),
        _FORM,
    ),
    "json": lambda token: (
        f'{{"logout_token": "{token}"}}'.encode(),
        "application/json",
    ),
    "plain-text": lambda token: (
        urlencode({"logout_token": token}).encode(),
        "text/plain",
    ),
}


@pytest.mark.parametrize("shape", _NOT_ONE_FORM.values(), ids=_NOT_ONE_FORM)
def test_a_body_that_is_not_one_logout_token_form_ends_nothing(
    world: World, shape: Callable[[str], tuple[bytes, str]]
) -> None:
    body, content_type = shape(world.token())

    answer = world.keycloak().post(
        BACKCHANNEL_LOGOUT_PATH, content=body, headers={"content-type": content_type}
    )

    assert answer.status_code == HTTPStatus.BAD_REQUEST
    _untouched(world)


def test_a_failing_account_store_answers_400_and_logs_the_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    breakdown = Breakdown()
    world = _arranged(
        tmp_path, accounts=lambda real: AccountsThatBreak(real, breakdown)
    )
    breakdown.now = True

    with caplog.at_level(logging.ERROR, logger=_BACKCHANNEL_ERROR):
        answer = world.logout_with(world.keycloak(), world.token())

    assert answer.status_code == HTTPStatus.BAD_REQUEST
    assert world.sessions_of("subject-1") == _ROWS_OF_SUBJECT_1
    assert world.scene.audit.events == []
    (record,) = [r for r in caplog.records if r.name == _BACKCHANNEL_ERROR]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], sqlite3.OperationalError)


def test_a_failing_audit_line_answers_400_after_the_sessions_already_ended(
    tmp_path: Path,
) -> None:
    breakdown = Breakdown()
    world = _arranged(
        tmp_path, audit=lambda recording: AuditThatBreaks(recording, breakdown)
    )
    breakdown.now = True

    answer = world.logout_with(world.keycloak(), world.token())

    assert answer.status_code == HTTPStatus.BAD_REQUEST
    assert world.sessions_of("subject-1") == 0


def test_the_sessions_end_in_one_hold_off_the_event_loop_before_the_answer_starts(
    tmp_path: Path,
) -> None:
    world = _arranged(tmp_path)
    log = world.lock.log

    async def lobby_that_logs_the_answer(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        async def logging_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                log.append("response-start")
            await send(message)

        await world.scene.lobby.app(scope, receive, logging_send)

    keycloak = TestClient(lobby_that_logs_the_answer, base_url=A_BROWSERS_ADDRESS)
    world.logout_with(keycloak, world.token())

    assert log == [("held", False), ("leaving", 1), "response-start"]


def test_the_lock_is_never_held_for_a_call_that_is_refused_before_the_delete(
    world: World,
) -> None:
    world.logout_with(
        world.keycloak(),
        world.scene.issuer.mint_logout_token(
            subject="subject-1", claims={"nonce": "n"}
        ),
    )
    world.keycloak().post(BACKCHANNEL_LOGOUT_PATH, content=b"")

    assert world.lock.log == []


def test_the_call_reaches_the_lobby_under_an_internal_name_over_plain_http(
    world: World,
) -> None:
    answer = world.logout_with(
        world.keycloak(base_url=_KEYCLOAK_BY_NAME), world.token()
    )

    assert answer.status_code == HTTPStatus.OK
    assert world.sessions_of("subject-1") == 0


def test_a_cross_site_post_with_a_session_cookie_is_judged_by_its_token_not_refused(
    world: World,
) -> None:
    answer = world.first.post(
        BACKCHANNEL_LOGOUT_PATH,
        content=urlencode({"logout_token": "garbage"}),
        headers={
            "content-type": _FORM,
            "sec-fetch-site": "cross-site",
            "origin": "https://evil.example",
        },
    )

    assert answer.status_code == HTTPStatus.BAD_REQUEST
    assert world.first.get("/").status_code == HTTPStatus.OK


_NEIGHBOURS = [
    BACKCHANNEL_LOGOUT_PATH + "/",
    BACKCHANNEL_LOGOUT_PATH + "/x",
    BACKCHANNEL_LOGOUT_PATH + "x",
    "/auth/%2e/backchannel-logout",
]


@pytest.mark.parametrize("path", _NEIGHBOURS)
def test_no_neighbour_of_the_exact_path_is_exempt_or_public(
    world: World, path: str
) -> None:
    token = world.token()
    from_a_server = world.logout_with(world.keycloak(), token, path=path)
    from_the_lobbys_origin = world.keycloak().post(
        path,
        content=urlencode({"logout_token": token}),
        headers={
            "content-type": _FORM,
            "origin": A_BROWSERS_ADDRESS,
            "accept": "application/json",
        },
    )

    assert from_a_server.status_code == HTTPStatus.FORBIDDEN
    assert from_the_lobbys_origin.status_code == HTTPStatus.UNAUTHORIZED
    _untouched(world)


def test_a_get_to_the_route_is_gated_while_signed_out(world: World) -> None:
    keycloak = world.keycloak()

    as_a_page = keycloak.get(
        BACKCHANNEL_LOGOUT_PATH,
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    as_json = keycloak.get(
        BACKCHANNEL_LOGOUT_PATH, headers={"accept": "application/json"}
    )

    assert as_a_page.status_code == HTTPStatus.FOUND
    assert as_a_page.headers["location"].startswith("/login/oidc?next=")
    assert as_json.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    "method", [HTTPMethod.GET, HTTPMethod.PUT, HTTPMethod.PATCH, HTTPMethod.DELETE]
)
def test_a_signed_in_browser_gets_405_for_every_method_but_post(
    world: World, method: HTTPMethod
) -> None:
    answer = world.first.request(
        method,
        BACKCHANNEL_LOGOUT_PATH,
        content=urlencode({"logout_token": world.token()}),
        headers={"content-type": _FORM},
    )

    assert answer.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    _untouched(world)


def test_a_body_declared_over_32_kib_gets_413_before_the_route(world: World) -> None:
    answer = world.keycloak().post(
        BACKCHANNEL_LOGOUT_PATH,
        content=b"logout_token=",
        headers={
            "content-type": _FORM,
            "content-length": str(_A_THIRD_OF_A_MEGABYTE),
        },
    )

    assert answer.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["content-security-policy"] == a_content_security_policy()
    _untouched(world)


def test_a_body_of_exactly_32_kib_is_read_and_refused_as_a_token(
    world: World,
) -> None:
    prefix = b"logout_token="
    body = prefix + b"a" * (_THE_CAP - len(prefix))

    answer = world.keycloak().post(
        BACKCHANNEL_LOGOUT_PATH, content=body, headers={"content-type": _FORM}
    )

    assert len(body) == _THE_CAP
    assert answer.status_code == HTTPStatus.BAD_REQUEST
    _untouched(world)
