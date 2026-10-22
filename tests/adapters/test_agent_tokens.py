"""The person's and the workload's tokens: refusals, rotation, cancellation, leaks."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.adapters.agent_tokens import PersonAccessTokens, WorkloadToken
from presentator.adapters.identity import SqliteSessionRecordStore
from presentator.contracts.copresenter import (
    AccessRefusal,
    CoPresenterUnavailableError,
    PersonToken,
)
from tests.adapters.agent_service_support import (
    GOOD_ANSWER,
    ReceivedCredentials,
    ask_once,
    event_frames,
    fake_service,
    reply_with,
    whole,
)
from tests.leak_support import everything_shown
from tests.sign_in_support import Scene, a_scene

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Iterator
    from pathlib import Path

    from pydantic import SecretStr

    from presentator.adapters.identity import StoredSession
    from presentator.ports.copresenter import PersonAccess

PERSON_ACCESS = "person-access-MARKER"
WORKLOAD_ACCESS = "workload-access-MARKER"
REFRESH = "refresh-MARKER"
ROTATED = "rotated-MARKER"
THREAD_ERROR = "thread-error-MARKER"
MARKERS = (PERSON_ACCESS, WORKLOAD_ACCESS, REFRESH, ROTATED, THREAD_ERROR)
LOGGER = "presentator.adapters.agent_tokens"
_THREAD_WAIT_SECONDS = 10


class Arranged:
    """A signed-in session over a fake issuer, and the two token sources on it."""

    def __init__(self, scene: Scene, *, refresh_token: str | None) -> None:
        """Sign a person in; their one session row is the one asked about."""
        scene.issuer.issue_person_access_token(PERSON_ACCESS)
        scene.issuer.issue_workload_access_token(WORKLOAD_ACCESS)
        if refresh_token is not None:
            scene.issuer.issue_refresh_token(refresh_token)
        scene.sign_in()
        (self.session_id,) = scene.stored_sessions()
        self.scene = scene
        self.person: PersonAccess = PersonAccessTokens(
            scene.relying_party, scene.sessions, scene.config
        )
        self.workload = WorkloadToken(scene.relying_party)

    def grants_so_far(self) -> int:
        """How many token requests the issuer has answered."""
        return len(self.scene.issuer.grant_types)


@pytest.fixture
def arranged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> Iterator[Arranged]:
    """A signed-in session; its test ends with no marker in any record at DEBUG."""
    caplog.set_level(logging.DEBUG)
    yield Arranged(a_scene(tmp_path), refresh_token=REFRESH)
    assert not _markers_seen(caplog)


def _markers_seen(caplog: pytest.LogCaptureFixture) -> list[str]:
    # caplog.records is only the phase running now; each phase is read by name.
    records = [
        record
        for phase in ("setup", "call", "teardown")
        for record in caplog.get_records(phase)  # pyright: ignore[reportArgumentType]
    ]
    shown = everything_shown(records)
    return [marker for marker in MARKERS if marker in shown]


@pytest.fixture
def seen_at_teardown(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Defect (#452 cell walk): the teardown leak walk reads the call phase's records."""
    caplog.set_level(logging.DEBUG)
    yield
    assert _markers_seen(caplog) == [PERSON_ACCESS]


def test_a_marker_logged_during_the_test_is_seen_at_teardown(
    seen_at_teardown: None,
) -> None:
    """Defect (#452 cell walk): the teardown walk sees a marker logged in the call phase."""
    assert seen_at_teardown is None
    logging.getLogger(LOGGER).warning("token %s", PERSON_ACCESS)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


def _failure_of(work: Awaitable[object]) -> CoPresenterUnavailableError:
    async def drive() -> object:
        return await work

    driven = drive()
    with pytest.raises(CoPresenterUnavailableError) as caught:
        asyncio.run(driven)
    error = caught.value
    assert (error.__cause__, error.__context__) == (None, None)
    assert not [m for m in MARKERS if m in everything_shown(errors=[error])]
    return error


def test_a_signed_in_session_gets_a_person_token_and_the_workload_its_own(
    arranged: Arranged,
) -> None:
    """Cell 1 (#452): the happy path."""
    person = asyncio.run(arranged.person.fresh(arranged.session_id))
    workload = asyncio.run(arranged.workload())

    assert person == PersonToken(PERSON_ACCESS)
    assert workload == WORKLOAD_ACCESS
    assert arranged.scene.stored_tokens() == [REFRESH]


def test_the_workload_token_is_fetched_on_every_call(arranged: Arranged) -> None:
    """Cell 1 (#452): the workload source keeps no token between calls."""
    asyncio.run(arranged.workload())
    first = arranged.scene.issuer.grant_types.count("client_credentials")
    asyncio.run(arranged.workload())

    assert arranged.scene.issuer.grant_types.count("client_credentials") == first + 1


def _without_a_refresh_token(arranged: Arranged) -> None:
    with closing(sqlite3.connect(arranged.scene.database)) as connection, connection:
        connection.execute("UPDATE sessions SET refresh_token = NULL")


def _revoked(arranged: Arranged) -> None:
    arranged.scene.issuer.revoke_refresh_token(REFRESH)


def _gone(arranged: Arranged) -> None:
    arranged.scene.sessions.delete(arranged.session_id)


def _past_the_cap(arranged: Arranged) -> None:
    arranged.scene.clock.advance(timedelta(hours=13))


@pytest.mark.parametrize(
    ("break_it", "expected", "asks_the_issuer"),
    [
        (_without_a_refresh_token, AccessRefusal.SIGN_IN_AGAIN, False),
        (_revoked, AccessRefusal.SIGN_IN_AGAIN, True),
        (_gone, AccessRefusal.SESSION_ENDED, False),
        (_past_the_cap, AccessRefusal.SESSION_ENDED, False),
    ],
    ids=["no-refresh-token", "invalid-grant", "row-gone", "past-the-cap"],
)
def test_a_session_without_a_standing_token_gets_its_refusal(
    arranged: Arranged,
    break_it: Callable[[Arranged], None],
    expected: AccessRefusal,
    asks_the_issuer: bool,  # noqa: FBT001
) -> None:
    """Cells 2-5 (#452): each refusal and whether the issuer was asked."""
    break_it(arranged)
    before = arranged.grants_so_far()

    refusal = asyncio.run(arranged.person.fresh(arranged.session_id))

    assert refusal is expected
    assert arranged.grants_so_far() - before == int(asks_the_issuer)


def test_an_issuer_that_is_down_is_one_warning_naming_the_reason(
    arranged: Arranged, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 6 (#452): an unavailable issuer is one warning."""
    arranged.scene.issuer.token_endpoint_is_down = True

    refusal = asyncio.run(arranged.person.fresh(arranged.session_id))

    assert refusal is AccessRefusal.ISSUER_UNAVAILABLE
    (warning,) = _warnings(caplog)
    assert "token-endpoint-failed" in warning


def test_a_rotated_refresh_token_is_stored_and_the_new_access_token_returned(
    arranged: Arranged,
) -> None:
    """Cell 7 (#452): the rotation is stored."""
    arranged.scene.issuer.rotate_refresh_token_to(ROTATED)

    person = asyncio.run(arranged.person.fresh(arranged.session_id))

    assert person == PersonToken(PERSON_ACCESS)
    assert arranged.scene.stored_tokens() == [ROTATED]


def test_a_row_deleted_between_the_two_transactions_ends_the_session(
    arranged: Arranged,
) -> None:
    """Cell 8 (#452): a row deleted mid-rotation ends the session."""
    arranged.scene.issuer.rotate_refresh_token_to(ROTATED)
    arranged.scene.issuer.during_token_request = lambda: _gone(arranged)

    refusal = asyncio.run(arranged.person.fresh(arranged.session_id))

    assert refusal is AccessRefusal.SESSION_ENDED
    assert arranged.scene.stored_tokens() == []


def test_a_workload_the_issuer_refuses_is_one_warning_naming_reason_and_code(
    arranged: Arranged, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 9 (#452): a refused workload."""
    arranged.scene.issuer.workload_is_refused = True

    _failure_of(arranged.workload())

    (warning,) = _warnings(caplog)
    assert "token-request-refused" in warning
    assert "invalid_client" in warning


def _raise_in_the_thread(error: Exception) -> Callable[[], None]:
    def hook() -> None:
        raise error

    return hook


def test_a_workload_thread_that_raises_is_one_warning_naming_only_its_type(
    arranged: Arranged, caplog: pytest.LogCaptureFixture
) -> None:
    """Cells 10, 15, 16 (#452): a raising workload thread leaks nothing."""
    error = RuntimeError(THREAD_ERROR)
    error.__context__ = RuntimeError(THREAD_ERROR)
    arranged.scene.issuer.during_token_request = _raise_in_the_thread(error)

    _failure_of(arranged.workload())

    assert _warnings(caplog) == ["agent token source failed: RuntimeError"]


class _LoadFails(SqliteSessionRecordStore):
    def load(self, session_id: str) -> StoredSession | None:
        error = RuntimeError(REFRESH)
        error.__context__ = RuntimeError(REFRESH)
        raise error


class _ReplaceAnswersNoBool(SqliteSessionRecordStore):
    def replace_refresh_token(
        self, session_id: str, *, refresh_token: SecretStr
    ) -> Any:  # noqa: ANN401
        return None


@pytest.mark.parametrize(
    ("store", "named"),
    [(_LoadFails, "RuntimeError"), (_ReplaceAnswersNoBool, "TypeError")],
    ids=["load-raises", "replace-answers-a-non-bool"],
)
def test_a_person_thread_that_raises_is_one_warning_naming_only_its_type(
    arranged: Arranged,
    store: type[SqliteSessionRecordStore],
    named: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cells 11, 15, 16 (#452): a raising person thread leaks nothing."""
    arranged.scene.issuer.rotate_refresh_token_to(ROTATED)
    scene = arranged.scene
    person = PersonAccessTokens(
        scene.relying_party, store(scene.database), scene.config
    )

    _failure_of(person.fresh(arranged.session_id))

    assert _warnings(caplog) == [f"agent token source failed: {named}"]


class RecordingExecutor(ThreadPoolExecutor):
    """A thread pool that keeps each future it hands out."""

    def __init__(self) -> None:
        """Start with nothing recorded and no done callback."""
        super().__init__()
        self.recorded: list[Future[Any]] = []
        self.when_done: Callable[[], object] | None = None

    def submit[**Arguments, Result](
        self,
        fn: Callable[Arguments, Result],
        /,
        *args: Arguments.args,
        **kwargs: Arguments.kwargs,
    ) -> Future[Result]:
        """Run on the pool, and call `when_done` before asyncio hears of the result."""
        future = super().submit(fn, *args, **kwargs)
        self.recorded.append(future)
        if (when_done := self.when_done) is not None:
            future.add_done_callback(lambda _done: when_done())
        return future


def _cancelled_before_the_thread(arranged: Arranged) -> asyncio.CancelledError:
    async def drive() -> asyncio.CancelledError:
        task = asyncio.create_task(arranged.person.fresh(arranged.session_id))
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    return asyncio.run(drive())


def test_a_cancellation_before_the_thread_asks_no_issuer_and_leaves_no_chain(
    arranged: Arranged,
) -> None:
    """Cell 12 (#452): cancellation before the thread."""
    before = arranged.grants_so_far()

    error = _cancelled_before_the_thread(arranged)

    assert arranged.grants_so_far() == before
    assert arranged.scene.stored_tokens() == [REFRESH]
    assert (error.__cause__, error.__context__) == (None, None)


async def _cancel_while_the_thread_runs(
    arranged: Arranged, executor: RecordingExecutor
) -> asyncio.CancelledError:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def hold_the_thread() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait(_THREAD_WAIT_SECONDS)

    arranged.scene.issuer.during_token_request = hold_the_thread
    task = asyncio.create_task(arranged.person.fresh(arranged.session_id))
    await entered.wait()
    task.cancel()
    release.set()
    await asyncio.wrap_future(executor.recorded[0])
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    return caught.value


async def _cancel_as_the_thread_returns(
    arranged: Arranged, executor: RecordingExecutor
) -> asyncio.CancelledError:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(arranged.person.fresh(arranged.session_id))
    executor.when_done = lambda: loop.call_soon_threadsafe(task.cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    await asyncio.wrap_future(executor.recorded[0])
    return caught.value


@pytest.mark.parametrize(
    "cancel",
    [_cancel_while_the_thread_runs, _cancel_as_the_thread_returns],
    ids=["inside-the-thread", "after-the-thread-returned"],
)
def test_a_cancelled_ask_still_stores_the_rotation_and_leaves_no_chain(
    arranged: Arranged,
    cancel: Callable[[Arranged, RecordingExecutor], Awaitable[asyncio.CancelledError]],
) -> None:
    """Cells 13-14 (#452): cancellation inside and after the thread."""
    arranged.scene.issuer.rotate_refresh_token_to(ROTATED)

    async def drive() -> asyncio.CancelledError:
        executor = RecordingExecutor()
        asyncio.get_running_loop().set_default_executor(executor)
        try:
            return await cancel(arranged, executor)
        finally:
            executor.shutdown(wait=True)

    error = asyncio.run(drive())

    assert arranged.scene.stored_tokens() == [ROTATED]
    assert (error.__cause__, error.__context__) == (None, None)


@contextmanager
def _every_logger_at_debug(caplog: pytest.LogCaptureFixture) -> Generator[None]:
    """Lower each named logger, and reach the ones the server's config cut off."""
    loggers = [
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    cut_off = [logger for logger in loggers if not logger.propagate]
    for logger in loggers:
        caplog.set_level(logging.DEBUG, logger=logger.name)
    for logger in cut_off:
        logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        for logger in cut_off:
            logger.removeHandler(caplog.handler)


def test_the_real_suppliers_feed_the_answerer_over_a_socket_and_leak_nothing(
    arranged: Arranged, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell 19 (#452): real socket, every logger at DEBUG."""
    arranged.scene.issuer.rotate_refresh_token_to(ROTATED)

    async def person() -> str:
        token = await arranged.person.fresh(arranged.session_id)
        assert isinstance(token, PersonToken)
        return token.value

    received: list[ReceivedCredentials] = []

    async def drive() -> list[str]:
        async with fake_service(
            lambda _question: reply_with(*whole(event_frames(*GOOD_ANSWER))),
            credentials_seen=received,
        ) as origin:
            with _every_logger_at_debug(caplog):
                answerer = ServiceAnswerer(
                    origin=origin, workload_token=arranged.workload
                )
                outcome = await ask_once(answerer, person, message="Frage?")
            assert outcome.error is None
            return outcome.texts

    assert asyncio.run(drive()) == ["Hallo ", "Welt."]
    assert received == [ReceivedCredentials(f"Bearer {PERSON_ACCESS}", WORKLOAD_ACCESS)]
    assert caplog.records
    assert arranged.scene.stored_tokens() == [ROTATED]
