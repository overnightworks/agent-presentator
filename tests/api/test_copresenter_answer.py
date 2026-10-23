"""The co-presenter answers through the agent service, from sign-in to the voice.

Every test asks as a signed-in admin of the talk host over real identity tables
and a fake issuer: the real token sources, the real service answerer over a fake
wire, the real sentence splitter and a fake voice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import httpx2
import pytest
from agent_client.wire import FailureCode, RejectionCode, RunRequest
from fastapi import FastAPI
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME, DEFAULT_SESSION_COOKIE_NAME

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.adapters.agent_tokens import PersonAccessTokens, WorkloadToken
from presentator.adapters.deck_texts import MirroredDeckTexts
from presentator.adapters.sentences import split_sentences
from presentator.api.copresenter import (
    NOT_ENABLED_REASON,
    CoPresenterSurface,
    add_copresenter_routes,
)
from presentator.application.copresenter import (
    AskLimiter,
    AskTicket,
    CoPresenterUse,
    RetryAfter,
)
from presentator.contracts.decks import Build, Deck, Source
from tests.adapters.agent_service_support import (
    GOOD_ANSWER,
    SERVICE_MESSAGE,
    Reply,
    Wire,
    assistant_text,
    cancelled,
    event_frames,
    failed,
    refusal_reply,
    reply_with,
    run_started,
    succeeded,
    violating_frame,
    whole,
)
from tests.adapters.test_decks import a_source, folders_under, mirrors_under
from tests.api.copresenter_support import (
    A_DECK as SUPPORT_DECK,
)
from tests.api.copresenter_support import (
    A_SOURCE,
    FakeDeckTexts,
    PrivateFakes,
    a_use,
    the_admins_decks,
)
from tests.api.lobby import (
    A_BROWSERS_HEADERS,
    A_TALK_ADDRESS,
    AN_AGENT_SERVICE,
    NOW,
)
from tests.api.talk_support import A_DECK, B_DECK, A, Hosts, two_hosts
from tests.application.fakes import FakeDeckStore, FakeSourceStore, FrozenClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from fastapi.testclient import TestClient
    from httpx2 import Response
    from starlette.types import ASGIApp, Message, Scope

    from presentator.adapters.identity import SqliteSessionRecordStore
    from presentator.api.auth import InstalledAuth
    from presentator.ports.copresenter import DeckTexts
    from tests.conftest import GitRemote
from tests.leak_support import everything_shown

PERSON: Final = "person-access-MARKER"
WORKLOAD: Final = "workload-access-MARKER"
REFRESH: Final = "refresh-MARKER"
ROTATED: Final = "rotated-MARKER"
TOKENS: Final = (PERSON, WORKLOAD, REFRESH, ROTATED)
ADMIN_GROUPS: Final = ["/apps/presentator", "/apps/presentator/admin"]
ASK: Final = "/copresenter/ask"
QUESTION: Final = {"said": "Was ist wichtig?", "slide": 1, "language": "de"}
THE_ASKED_DECK: Final = {**QUESTION, "deck": A_DECK}
FIRST_AUDIO: Final = "presentator.application.copresenter"
SERVICE_LOG: Final = "presentator.adapters.agent_service"


def a_stalled_transport() -> httpx2.AsyncBaseTransport:
    def stall(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout(PERSON, request=request)

    return httpx2.MockTransport(stall)


@dataclass
class RecordingLimiter(AskLimiter):
    """The real limiter, remembering which sessions asked it for a ticket."""

    session_ids: list[str] = field(default_factory=list[str])

    def admit(self, session_id: str) -> AskTicket | RetryAfter:
        self.session_ids.append(session_id)
        return super().admit(session_id)


@dataclass
class Run:
    """One ask by a signed-in admin, and everything that could have seen it."""

    response: Response
    hosts: Hosts
    wire: Wire
    fakes: PrivateFakes
    talk: TestClient
    limiter: RecordingLimiter
    grants_before: list[str] = field(default_factory=list[str])

    @property
    def events(self) -> list[str]:
        return re.findall(r"^event: (\w+)$", self.response.text, re.MULTILINE)

    @property
    def grants_since_sign_in(self) -> list[str]:
        return self.hosts.issuer.grant_types[len(self.grants_before) :]

    def error_payload(self) -> dict[str, str]:
        (payload,) = re.findall(
            r"^event: error\ndata: (.*)$", self.response.text, re.MULTILINE
        )
        return json.loads(payload)


def delete_the_sessions(hosts: Hosts) -> None:
    with closing(sqlite3.connect(hosts.database)) as connection:
        connection.execute("DELETE FROM sessions")
        connection.commit()


def demote_the_admin(hosts: Hosts) -> None:
    with closing(sqlite3.connect(hosts.database)) as connection:
        connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (A.id,))
        connection.commit()


def the_session_ends_during_the_grant(hosts: Hosts) -> None:
    hosts.issuer.during_token_request = lambda: delete_the_sessions(hosts)


def the_token_endpoint_goes_down(hosts: Hosts) -> None:
    hosts.issuer.token_endpoint_is_down = True


def the_token_thread_fails(hosts: Hosts) -> None:
    def fail() -> None:
        raise RuntimeError(PERSON)

    hosts.issuer.during_token_request = fail


def the_refresh_token_is_revoked(hosts: Hosts) -> None:
    hosts.issuer.revoke_refresh_token(REFRESH)


def the_workload_is_refused(hosts: Hosts) -> None:
    hosts.issuer.workload_is_refused = True


def nothing_happens(_hosts: Hosts) -> None:
    return None


@dataclass(frozen=True, kw_only=True)
class Scenario:
    """What differs from the happy path: one factor at a time."""

    reply: Reply = field(
        default_factory=lambda: reply_with(*whole(event_frames(*GOOD_ANSWER)))
    )
    refresh_token: str | None = REFRESH
    before_ask: Callable[[Hosts], None] = nothing_happens
    texts: DeckTexts | None = None
    delay: Callable[[Wire, Hosts, float], Awaitable[None]] | None = None
    transport: Callable[[], httpx2.AsyncBaseTransport] | None = None
    body: Mapping[str, object] = field(default_factory=lambda: dict(THE_ASKED_DECK))
    sources: list[Source] | None = None
    arrange: Callable[[Hosts], None] = nothing_happens
    speaking_fails_at: int | None = None


def drive(tmp_path: Path, scenario: Scenario = Scenario()) -> Run:  # noqa: B008
    """Sign an admin in at the talk host, then ask once."""
    wire = Wire(scenario.reply)
    limiter = RecordingLimiter(clock=FrozenClock(instant=NOW))
    fakes = PrivateFakes()
    fakes.speaking.fails_at = scenario.speaking_fails_at
    hosts_made: list[Hosts] = []

    async def wait(seconds: float) -> None:
        if scenario.delay is None:
            await asyncio.sleep(seconds)
        else:
            await scenario.delay(wire, hosts_made[0], seconds)

    def surface(
        store: FakeDeckStore, sessions: SqliteSessionRecordStore, auth: InstalledAuth
    ) -> CoPresenterSurface:
        return CoPresenterSurface(
            use=CoPresenterUse(
                answers=ServiceAnswerer(
                    origin=AN_AGENT_SERVICE,
                    workload_token=WorkloadToken(auth.relying_party),
                    transport=scenario.transport or wire.transport,
                ),
                speech=fakes.speech,
                decks=store,
                sources=FakeSourceStore(sources=scenario.sources or [A_SOURCE]),
                texts=scenario.texts or fakes.texts,
                boundaries=split_sentences,
                speaking=fakes.speaking,
                person_access=PersonAccessTokens(
                    auth.relying_party, sessions, auth.config
                ),
            ),
            limiter=limiter,
            browser_origin=A_TALK_ADDRESS,
            delay=wait,
        )

    hosts = two_hosts(tmp_path, copresenter=surface)
    hosts_made.append(hosts)
    hosts.issuer.issue_person_access_token(PERSON)
    hosts.issuer.issue_workload_access_token(WORKLOAD)
    hosts.issuer.rotate_refresh_token_to(ROTATED)
    if scenario.refresh_token is not None:
        hosts.issuer.issue_refresh_token(scenario.refresh_token)
    scenario.arrange(hosts)
    talk = hosts.browser(A_TALK_ADDRESS, headers=A_BROWSERS_HEADERS)
    hosts.sign_in(talk, A_TALK_ADDRESS, subject=A.id, groups=ADMIN_GROUPS)
    grants_before = list(hosts.issuer.grant_types)
    scenario.before_ask(hosts)
    response = ask(talk, scenario.body)
    return Run(response, hosts, wire, fakes, talk, limiter, grants_before)


def ask(talk: TestClient, body: Mapping[str, object] = THE_ASKED_DECK) -> Response:
    return talk.post(
        ASK,
        json=body,
        headers={"x-csrf-token": talk.cookies[DEFAULT_CSRF_COOKIE_NAME]},
    )


A_PUSH: Final = datetime(2026, 1, 15, 9, tzinfo=UTC)
ASKED_MARKER: Final = "asked-deck-MARKER"
OTHER_MARKER: Final = "other-deck-MARKER"
LATER_MARKER: Final = "later-commit-MARKER"
THE_ASKED: Final = "kern"


def the_asked_decks_words_at_its_built_commit(
    tmp_path: Path, remote: GitRemote, caplog: pytest.LogCaptureFixture
) -> Run:
    """A repository of two decks, the asked one built before it was edited."""
    remote.commit(
        {
            f"{THE_ASKED}/slides.md": f"# Eins\n{ASKED_MARKER}\n",
            "anderes/slides.md": f"# Fremd\n{OTHER_MARKER}\n",
        },
        at=A_PUSH,
    )
    built_at = remote.head
    remote.commit(
        {
            f"{THE_ASKED}/slides.md": (
                f"# Eins\n{ASKED_MARKER}\n\n---\n\n{LATER_MARKER}\n"
            )
        },
        at=A_PUSH,
    )
    source = replace(a_source(remote.url), id=A_SOURCE.id, owner_id=A.id)
    folders_under(tmp_path).folders(source)

    def own_the_built_deck(hosts: Hosts) -> None:
        hosts.store.put(
            Deck(
                slug=THE_ASKED,
                title="Kern",
                changed_at=NOW,
                owner_id=A.id,
                source_id=source.id,
                commit=built_at,
                build=None,
                attempt=None,
            )
        )
        hosts.store.put_build(
            THE_ASKED,
            Build(
                directory=tmp_path / "talk",
                pdf=tmp_path / "deck.pdf",
                commit=built_at,
                built_at=NOW,
            ),
        )

    caplog.set_level(logging.INFO, logger=FIRST_AUDIO)
    scenario = Scenario(
        texts=MirroredDeckTexts(mirrors=mirrors_under(tmp_path)),
        sources=[source],
        arrange=own_the_built_deck,
        body={**QUESTION, "deck": THE_ASKED},
    )
    return drive(tmp_path, scenario)


def test_a_signed_in_admins_ask_reaches_the_service_with_both_tokens_and_only_that_decks_words(  # noqa: E501
    tmp_path: Path, remote: GitRemote, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 1."""
    run = the_asked_decks_words_at_its_built_commit(tmp_path, remote, caplog)

    (request,) = run.wire.requests
    sent = RunRequest.model_validate_json(request.content)
    (message,) = sent.turn.messages
    assert request.headers["authorization"] == f"Bearer {PERSON}"
    assert request.headers["agent-workload-token"] == WORKLOAD
    assert sent.tools == {}
    assert ASKED_MARKER in message.text
    assert OTHER_MARKER not in message.text
    assert LATER_MARKER not in message.text
    assert run.response.status_code == HTTPStatus.OK
    assert run.events == ["text", "text", "sentence", "audio", "done"]
    (line,) = [r.getMessage() for r in caplog.records if r.name == FIRST_AUDIO]
    assert re.fullmatch(r"co-presenter first audio: ms=\d+", line)
    assert the_ticket_was_released(run)


def the_ticket_was_released(run: Run) -> bool:
    """Whether every ticket the ask took is free again, so its session may ask."""
    assert run.limiter.session_ids
    for session_id in set(run.limiter.session_ids):
        ticket = run.limiter.admit(session_id)
        if not isinstance(ticket, AskTicket):
            return False
        ticket.release()
    return True


@pytest.mark.parametrize(
    ("setup", "status", "detail"),
    [
        pytest.param(
            Scenario(refresh_token=None),
            HTTPStatus.UNAUTHORIZED,
            "Sign in again",
            id="no-refresh-token",
        ),
        pytest.param(
            Scenario(before_ask=the_refresh_token_is_revoked),
            HTTPStatus.UNAUTHORIZED,
            "Sign in again",
            id="invalid-grant",
        ),
        pytest.param(
            Scenario(before_ask=the_session_ends_during_the_grant),
            HTTPStatus.UNAUTHORIZED,
            "Not authenticated",
            id="session-ended",
        ),
        pytest.param(
            Scenario(before_ask=the_token_endpoint_goes_down),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Co-presenter unavailable",
            id="issuer-unavailable",
        ),
        pytest.param(
            Scenario(before_ask=the_token_thread_fails),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Co-presenter unavailable",
            id="token-thread-fails",
        ),
    ],
)
def test_a_person_token_refusal_is_answered_before_the_service_is_called(
    tmp_path: Path, setup: Scenario, status: HTTPStatus, detail: str
) -> None:
    """Cells 2 to 5."""
    run = drive(tmp_path, setup)

    assert run.response.status_code == status
    assert run.response.json() == {"detail": detail}
    assert run.wire.requests == []
    assert run.fakes.speaking.spoken == []
    assert the_ticket_was_released(run)


def test_a_session_that_ended_during_the_grant_asked_the_issuer_exactly_once(
    tmp_path: Path,
) -> None:
    """Cell 3: the grant ran, and nothing was spent after it."""
    run = drive(tmp_path, Scenario(before_ask=the_session_ends_during_the_grant))

    assert run.grants_since_sign_in == ["refresh_token"]


@pytest.mark.parametrize(
    "before_ask",
    [
        pytest.param(the_refresh_token_is_revoked, id="invalid-grant"),
        pytest.param(the_token_endpoint_goes_down, id="issuer-unavailable"),
        pytest.param(the_token_thread_fails, id="token-thread-fails"),
        pytest.param(the_session_ends_during_the_grant, id="session-ended"),
    ],
)
def test_the_limiter_ticket_is_released_after_each_person_token_refusal(
    tmp_path: Path, before_ask: Callable[[Hosts], None]
) -> None:
    """Cells 2 to 5: the ticket the ask took is free again."""
    run = drive(tmp_path, Scenario(before_ask=before_ask))
    run.hosts.issuer.token_endpoint_is_down = False

    assert run.response.status_code != HTTPStatus.OK
    assert the_ticket_was_released(run)


def test_a_doubled_session_cookie_is_refused_before_any_token_is_fetched(
    tmp_path: Path,
) -> None:
    """Cell 6: webauth reads a doubled cookie as no session, so its CSRF layer refuses.

    The refusal is the library's 403 before the route runs; the route's own
    401 "Not authenticated" for a missing admission is pinned in
    test_copresenter.py.
    """
    run = drive(tmp_path)
    grants = len(run.hosts.issuer.grant_types)
    session = run.talk.cookies[DEFAULT_SESSION_COOKIE_NAME]
    csrf = run.talk.cookies[DEFAULT_CSRF_COOKIE_NAME]
    named = f"{DEFAULT_SESSION_COOKIE_NAME}={session}"

    refused = run.talk.post(
        ASK,
        json=THE_ASKED_DECK,
        headers={
            "cookie": f"{named}; {named}; {DEFAULT_CSRF_COOKIE_NAME}={csrf}",
            "x-csrf-token": csrf,
        },
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert refused.json() == {"detail": "CSRF token missing or invalid"}
    assert the_browser_saw_no_token(refused)
    assert len(run.wire.requests) == 1
    assert len(run.hosts.issuer.grant_types) == grants


def test_a_refused_workload_token_ends_in_a_plain_error_and_never_posts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 7."""
    caplog.set_level(logging.INFO, logger=FIRST_AUDIO)

    run = drive(tmp_path, Scenario(before_ask=the_workload_is_refused))

    assert run.events == ["error"]
    assert "reason" not in run.error_payload()
    assert run.wire.requests == []
    assert not [r for r in caplog.records if r.name == FIRST_AUDIO]
    assert the_ticket_was_released(run)


def test_a_service_that_does_not_enable_the_person_ends_in_error_with_its_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 8: F2's overlay reads the literal `not-enabled`, so it is spelled out."""
    caplog.set_level(logging.WARNING, logger=SERVICE_LOG)

    run = drive(tmp_path, Scenario(reply=refusal_reply(RejectionCode.NOT_ENABLED, 403)))

    assert run.events == ["error"]
    assert run.error_payload() == {
        "message": "the answer could not be spoken",
        "reason": "not-enabled",
    }
    assert NOT_ENABLED_REASON == "not-enabled"
    assert any("not_enabled" in r.getMessage() for r in caplog.records)
    assert the_ticket_was_released(run)


def test_a_busy_service_ends_in_error_without_a_reason_and_logs_the_wait(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 9."""
    caplog.set_level(logging.WARNING, logger=SERVICE_LOG)

    busy = refusal_reply(RejectionCode.BUSY, 429, **{"retry-after": "7"})

    run = drive(tmp_path, Scenario(reply=busy))

    assert run.events == ["error"]
    assert "reason" not in run.error_payload()
    assert any(
        "busy" in r.getMessage() and "retry_after=7" in r.getMessage()
        for r in caplog.records
    )
    assert the_ticket_was_released(run)


def test_a_service_that_refuses_the_token_ends_in_error_without_a_reason(
    tmp_path: Path,
) -> None:
    """Cell 10."""
    run = drive(
        tmp_path, Scenario(reply=refusal_reply(RejectionCode.UNAUTHORIZED, 401))
    )

    assert run.events == ["error"]
    assert "reason" not in run.error_payload()
    assert the_ticket_was_released(run)


@pytest.mark.parametrize(
    ("setup", "events"),
    [
        pytest.param(
            Scenario(
                reply=reply_with(
                    *whole(
                        event_frames(
                            run_started(),
                            assistant_text("Ein Satz. Noch einer"),
                            failed(FailureCode.PROVIDER_ERROR),
                        )
                    )
                )
            ),
            ["text", "sentence", "audio", "error"],
            id="failed-after-text",
        ),
        pytest.param(
            Scenario(
                reply=reply_with(*whole(event_frames(run_started(), cancelled())))
            ),
            ["error"],
            id="cancelled",
        ),
        pytest.param(
            Scenario(reply=reply_with(violating_frame())),
            ["error"],
            id="stream-violation",
        ),
        pytest.param(
            Scenario(transport=a_stalled_transport),
            ["error"],
            id="stall",
        ),
        pytest.param(
            Scenario(reply=Reply(refuses_connection=True)),
            ["error"],
            id="refused-connection",
        ),
    ],
)
def test_every_other_service_failure_ends_in_a_plain_error_and_never_in_done(
    tmp_path: Path, setup: Scenario, events: list[str]
) -> None:
    """Cells 11 to 15."""
    run = drive(tmp_path, setup)

    assert run.events == events
    assert "reason" not in run.error_payload()
    assert the_ticket_was_released(run)


def test_speech_failing_on_the_second_sentence_ends_after_the_first_in_error(
    tmp_path: Path,
) -> None:
    """Cell 16."""
    reply = reply_with(
        *whole(
            event_frames(
                run_started(),
                assistant_text("Erster Satz. Zweiter Satz. Dritter."),
                succeeded(),
            )
        )
    )

    run = drive(tmp_path, Scenario(reply=reply, speaking_fails_at=1))

    assert run.events == ["text", "sentence", "audio", "sentence", "error"]
    assert "reason" not in run.error_payload()
    assert the_ticket_was_released(run)


HANGING_AFTER_THE_FIRST_WORDS: Final = Reply(
    chunks=whole(event_frames(run_started(), assistant_text("Erst "))),
    hangs_after_chunks=True,
)


@dataclass
class ChangeOnceTheAnswerHangs:
    """A session re-check that changes the world once the service has hung."""

    change: Callable[[Hosts], None]
    waits: int = 0

    async def __call__(self, wire: Wire, hosts: Hosts, _seconds: float) -> None:
        self.waits += 1
        await asyncio.wait_for(wire.response_stream.hung.wait(), timeout=5)
        self.change(hosts)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(delete_the_sessions, id="session-ended"),
        pytest.param(demote_the_admin, id="admin-role-lost"),
    ],
)
def test_an_answer_ends_within_one_recheck_when_the_session_or_the_role_goes(
    tmp_path: Path, change: Callable[[Hosts], None]
) -> None:
    """Cells 17 and 19: the service's connection is closed behind it."""
    recheck = ChangeOnceTheAnswerHangs(change)
    scenario = Scenario(reply=HANGING_AFTER_THE_FIRST_WORDS, delay=recheck)

    run = drive(tmp_path, scenario)

    assert run.response.status_code == HTTPStatus.OK
    assert run.events == ["text"]
    assert recheck.waits == 1
    assert run.wire.response_stream.closed.is_set()
    assert the_ticket_was_released(run)


def _a_hanging_app(
    wire: Wire, limiter: AskLimiter, admission: Callable[[object], FixedSession]
) -> FastAPI:
    app = FastAPI()
    add_copresenter_routes(
        app,
        surface=CoPresenterSurface(
            use=replace(
                a_use(PrivateFakes(), the_admins_decks()),
                answers=ServiceAnswerer(
                    origin=AN_AGENT_SERVICE,
                    workload_token=a_workload_token,
                    transport=wire.transport,
                ),
            ),
            limiter=limiter,
            browser_origin=A_TALK_ADDRESS,
        ),
        admission=admission,
    )
    return app


def test_a_browser_that_leaves_mid_answer_closes_the_services_connection() -> None:
    """Cell 18."""
    wire = Wire(HANGING_AFTER_THE_FIRST_WORDS)
    limiter = AskLimiter(clock=FrozenClock(instant=NOW))
    app = _a_hanging_app(wire, limiter, lambda _connection: FixedSession())

    asyncio.run(_ask_and_leave_after_the_first_words(app))

    assert wire.response_stream.closed.is_set()
    assert isinstance(limiter.admit(FixedSession().session_id), AskTicket)


def test_the_admission_runs_off_the_event_loop_thread() -> None:
    """Decision 5: the session read of the admission belongs to the thread hop."""
    admitted_on: list[int] = []

    def admission(_connection: object) -> FixedSession:
        admitted_on.append(threading.get_ident())
        return FixedSession()

    app = _a_hanging_app(
        Wire(HANGING_AFTER_THE_FIRST_WORDS),
        AskLimiter(clock=FrozenClock(instant=NOW)),
        admission,
    )

    asyncio.run(_ask_and_leave_after_the_first_words(app))

    assert admitted_on
    assert threading.get_ident() not in admitted_on


async def a_workload_token() -> str:
    return WORKLOAD


@dataclass(frozen=True)
class FixedSession:
    """The admitted session of a browser."""

    session_id: str = "session"
    user_id: str = A.id
    is_admin: bool = True


async def _ask_and_leave_after_the_first_words(app: ASGIApp) -> None:
    first_words_sent = asyncio.Event()
    messages: list[Message] = [
        {
            "type": "http.request",
            "body": json.dumps({**QUESTION, "deck": SUPPORT_DECK}).encode(),
        }
    ]

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        await first_words_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_words_sent.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": ASK,
        "raw_path": ASK.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"talk.testserver"),
            (b"content-type", b"application/json"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)


@pytest.mark.parametrize(
    "texts",
    [
        pytest.param(FakeDeckTexts(unreadable=True), id="unreadable-mirror"),
        pytest.param(FakeDeckTexts(text=None), id="no-slides"),
    ],
)
def test_a_deck_without_readable_words_ends_in_error_and_never_calls_the_service(
    tmp_path: Path, texts: FakeDeckTexts
) -> None:
    """Cells 20 and 21."""
    run = drive(tmp_path, Scenario(texts=texts))

    assert run.events == ["error"]
    assert run.wire.requests == []
    assert the_ticket_was_released(run)


def test_another_admins_deck_gets_the_ghosts_refusal_and_spends_no_token(
    tmp_path: Path,
) -> None:
    """Cell 22: E11 holds before any token is fetched."""
    (tmp_path / "ghost").mkdir()
    (tmp_path / "foreign").mkdir()
    ghost = drive(tmp_path / "ghost", Scenario(body={**QUESTION, "deck": "ghost"}))
    foreign = drive(tmp_path / "foreign", Scenario(body={**QUESTION, "deck": B_DECK}))

    assert foreign.response.status_code == HTTPStatus.NOT_FOUND
    assert foreign.response.content == ghost.response.content
    assert foreign.grants_since_sign_in == []
    assert foreign.wire.requests == []


def test_an_answer_without_any_text_is_done_with_no_audio_and_logs_no_first_audio(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 27."""
    caplog.set_level(logging.INFO, logger=FIRST_AUDIO)
    nothing = reply_with(*whole(event_frames(run_started(), succeeded())))

    run = drive(tmp_path, Scenario(reply=nothing))

    assert run.events == ["done"]
    assert run.response.text == 'event: done\ndata: {"text": ""}\n\n'
    assert not [r for r in caplog.records if r.name == FIRST_AUDIO]
    assert the_ticket_was_released(run)


def _echoing_the_token() -> Reply:
    body = json.dumps({"detail": f"bad token {PERSON} {WORKLOAD}"}).encode()
    return Reply(
        status=401, headers={"content-type": "application/json"}, chunks=[body]
    )


LEAK_SCENARIOS: Final = {
    "happy": Scenario(),
    "sign-in-again": Scenario(refresh_token=None),
    "invalid-grant": Scenario(before_ask=the_refresh_token_is_revoked),
    "session-ended": Scenario(before_ask=the_session_ends_during_the_grant),
    "issuer-unavailable": Scenario(before_ask=the_token_endpoint_goes_down),
    "token-thread-fails": Scenario(before_ask=the_token_thread_fails),
    "workload-refused": Scenario(before_ask=the_workload_is_refused),
    "not-enabled": Scenario(reply=refusal_reply(RejectionCode.NOT_ENABLED, 403)),
    "busy": Scenario(
        reply=refusal_reply(RejectionCode.BUSY, 429, **{"retry-after": "7"})
    ),
    "service-echoes-the-tokens": Scenario(reply=_echoing_the_token()),
    "failed-after-text": Scenario(
        reply=reply_with(
            *whole(
                event_frames(
                    run_started(),
                    assistant_text("Ein Satz."),
                    failed(FailureCode.PROVIDER_ERROR),
                )
            )
        )
    ),
    "cancelled": Scenario(
        reply=reply_with(*whole(event_frames(run_started(), cancelled())))
    ),
    "stream-violation": Scenario(reply=reply_with(violating_frame())),
    "speech-fails-on-the-second-sentence": Scenario(
        reply=reply_with(
            *whole(
                event_frames(
                    run_started(),
                    assistant_text("Erster Satz. Zweiter Satz. Dritter."),
                    succeeded(),
                )
            )
        ),
        speaking_fails_at=1,
    ),
    "stall": Scenario(transport=a_stalled_transport),
    "refused-connection": Scenario(reply=Reply(refuses_connection=True)),
}


def the_browser_saw_no_token(response: Response) -> bool:
    shown = f"{response.headers}\n{response.text}"
    return not any(marker in shown for marker in (*TOKENS, SERVICE_MESSAGE))


@pytest.mark.parametrize("scenario", LEAK_SCENARIOS.values(), ids=LEAK_SCENARIOS)
def test_no_token_reaches_a_log_record_or_the_browser(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scenario: Scenario
) -> None:
    """Cells 23 and 24: every logger at DEBUG, and the whole of what the browser got."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpcore2")

    run = drive(tmp_path, scenario)

    logged = everything_shown(caplog.records)
    assert the_browser_saw_no_token(run.response)
    for marker in TOKENS:
        assert marker not in logged
