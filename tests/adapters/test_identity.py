"""The identity bridge against a temporary SQLite file."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webauth.liveness import IdleWindowLiveness

from presentator.adapters.identity import (
    IdleWindow,
    SqliteLoginAttemptStore,
    SqliteSessionRecordStore,
    SqliteUserStore,
    SystemClock,
    create_identity_tables,
)
from presentator.application.identity import IDLE_WINDOW
from presentator.contracts.models import (
    Account,
    FirstStartClosedError,
    Role,
    Session,
    User,
)
from tests.application.fakes import FrozenClock

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_STORED_HASH = "a hash of something else"
_THE_OLD_HASH = "the hash a reset replaces"
_THE_NEW_HASH = "the hash a reset writes"
_ADDRESS = "203.0.113.7"
_AGENT = "TestBrowser/1.0"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    return database


def an_admin(user_id: str = "user-1", username: str = "felix") -> User:
    return User(id=user_id, username=username, role=Role.ADMIN)


def an_account(
    user_id: str = "user-1",
    username: str = "felix",
    password_hash: str = _STORED_HASH,
) -> Account:
    return Account(
        id=user_id,
        username=username,
        role=Role.ADMIN,
        password_hash=password_hash,
    )


def test_the_database_keeps_its_write_ahead_log(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"


def test_an_account_is_read_back_by_name_with_the_hash_it_was_stored_with(
    database: Path,
) -> None:
    users = SqliteUserStore(database)
    users.add_first_account(an_account())

    account = users.get_by_username("felix")

    assert account is not None
    assert account.as_user() == an_admin()
    assert account.password_hash == _STORED_HASH


def test_an_account_is_read_back_by_id_and_counted(database: Path) -> None:
    users = SqliteUserStore(database)
    assert users.count() == 0

    users.add_first_account(an_account())

    assert users.get("user-1") == an_admin()
    assert users.count() == 1


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(SqliteUserStore.get, id="by-id"),
        pytest.param(SqliteUserStore.get_by_username, id="by-name"),
    ],
)
def test_an_account_that_does_not_exist_reads_as_nothing(
    database: Path,
    read: Callable[[SqliteUserStore, str], User | Account | None],
) -> None:
    assert read(SqliteUserStore(database), "nobody") is None


def test_a_created_account_is_read_back(database: Path) -> None:
    users = SqliteUserStore(database)
    users.add_first_account(an_account())

    created = users.create("anna", "hash-of-annas", Role.USER.value)

    assert users.get_by_username("anna") == created
    assert created.role is Role.USER
    assert created.is_active is True


def test_a_touched_session_keeps_its_row_and_moves_its_last_seen(
    database: Path,
) -> None:
    clock = FrozenClock(instant=_NOW)
    sessions = SqliteSessionRecordStore(database, clock=clock)
    session = sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )

    later = _NOW + timedelta(minutes=15)
    renewed = sessions.touch(
        session,
        ip_address="198.51.100.8",
        user_agent="LaterBrowser/2.0",
        now=later,
    )

    stored = sessions.load(session.id)
    assert renewed is True
    assert stored is not None
    assert stored.last_seen == later
    assert stored.ip_address == "198.51.100.8"
    assert stored.user_agent == "LaterBrowser/2.0"


def test_a_session_deleted_after_its_load_is_not_brought_back_by_touch(
    database: Path,
) -> None:
    clock = FrozenClock(instant=_NOW)
    request = SqliteSessionRecordStore(database, clock=clock)
    logout = SqliteSessionRecordStore(database, clock=clock)
    session = request.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )

    loaded = request.load(session.id)
    assert loaded is not None
    logout.delete(session.id)
    renewed = request.touch(
        loaded,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
        now=_NOW + timedelta(minutes=1),
    )

    assert renewed is False
    assert request.load(session.id) is None


def test_a_removed_session_is_gone(database: Path) -> None:
    sessions = SqliteSessionRecordStore(database, clock=FrozenClock(instant=_NOW))
    session = sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )

    sessions.delete(session.id)

    assert sessions.load(session.id) is None


def test_deleting_one_persons_sessions_leaves_another_persons_standing(
    database: Path,
) -> None:
    sessions = SqliteSessionRecordStore(database, clock=FrozenClock(instant=_NOW))
    kept = sessions.create(
        "user-2",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )
    sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )

    dropped = sessions.delete_for_user("user-1")

    assert dropped == 1
    assert sessions.load(kept.id) is not None


def test_overflow_sessions_drop_the_oldest_and_keep_the_newest(
    database: Path,
) -> None:
    clock = FrozenClock(instant=_NOW)
    sessions = SqliteSessionRecordStore(database, clock=clock)
    oldest = sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )
    clock.advance(timedelta(minutes=1))
    middle = sessions.create(
        "user-1",
        clock.now() + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )
    clock.advance(timedelta(minutes=1))
    newest = sessions.create(
        "user-1",
        clock.now() + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )

    dropped = sessions.prune_overflow("user-1", max_sessions=2)

    assert dropped == [oldest.id]
    assert sessions.load(oldest.id) is None
    assert sessions.load(middle.id) is not None
    assert sessions.load(newest.id) is not None


def test_failed_attempts_are_counted_only_inside_the_window(database: Path) -> None:
    clock = FrozenClock(instant=_NOW - timedelta(hours=1))
    attempts = SqliteLoginAttemptStore(database, clock=clock)
    attempts.record(ip_address=_ADDRESS, username="felix", success=False)
    clock.advance(timedelta(hours=1))
    attempts.record(ip_address=_ADDRESS, username="felix", success=False)
    attempts.record(ip_address="198.51.100.8", username="someone-else", success=False)
    felix_failures = ("older", "newer")
    day = int(timedelta(days=1).total_seconds())

    assert (
        attempts.count_recent_failures(
            ip_address=_ADDRESS,
            window_seconds=int(timedelta(minutes=5).total_seconds()),
            username="felix",
        )
        == 1
    )
    assert attempts.count_recent_failures(
        ip_address=_ADDRESS,
        window_seconds=day,
        username="felix",
    ) == len(felix_failures)
    from_this_address = attempts.count_recent_failures(
        ip_address=_ADDRESS,
        window_seconds=day,
    )
    assert from_this_address == len(felix_failures)


def test_a_successful_attempt_does_not_spend_the_budget(database: Path) -> None:
    attempts = SqliteLoginAttemptStore(database, clock=FrozenClock(instant=_NOW))

    attempts.record(ip_address=_ADDRESS, username="felix", success=True)

    assert (
        attempts.count_recent_failures(
            ip_address=_ADDRESS,
            window_seconds=300,
            username="felix",
        )
        == 0
    )


def test_the_system_clock_reads_an_aware_utc_time() -> None:
    assert SystemClock().now().tzinfo == UTC


def test_a_second_first_account_is_refused_even_after_an_empty_count(
    database: Path,
) -> None:
    users = SqliteUserStore(database)
    assert users.count() == 0
    users.add_first_account(an_account())

    with pytest.raises(FirstStartClosedError, match="already has an account"):
        users.add_first_account(
            an_account(user_id="user-2", username="stranger"),
        )

    assert users.count() == 1
    assert users.get_by_username("stranger") is None


def test_the_idle_window_admits_a_session_at_the_boundary() -> None:
    session = Session(id="session-1", user_id="user-1", last_seen=_NOW)
    window = IdleWindow(IdleWindowLiveness(int(IDLE_WINDOW.total_seconds())))

    assert window.admits(session, at=_NOW + IDLE_WINDOW) is True
    assert window.admits(session, at=_NOW + IDLE_WINDOW + timedelta(seconds=1)) is False


def test_creating_the_tables_twice_leaves_the_columns_as_they_were(
    database: Path,
) -> None:
    create_identity_tables(database)

    SqliteUserStore(database).add_first_account(an_account())

    assert SqliteUserStore(database).count() == 1


def test_pruning_below_the_cap_drops_nothing(database: Path) -> None:
    sessions = SqliteSessionRecordStore(database, clock=FrozenClock(instant=_NOW))
    session = sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )

    assert sessions.prune_overflow("user-1", max_sessions=5) == []
    assert sessions.load(session.id) is not None


def test_deleting_sessions_for_nobody_drops_nothing(database: Path) -> None:
    sessions = SqliteSessionRecordStore(database, clock=FrozenClock(instant=_NOW))

    assert sessions.delete_for_user("nobody") == 0


def test_replacing_the_password_ends_every_session_and_counts_them(
    database: Path,
) -> None:
    users = SqliteUserStore(database)
    users.add_first_account(an_account(password_hash=_THE_OLD_HASH))
    sessions = SqliteSessionRecordStore(database, clock=FrozenClock(instant=_NOW))
    opened = (
        sessions.create(
            "user-1",
            _NOW + IDLE_WINDOW,
            ip_address=_ADDRESS,
            user_agent=_AGENT,
        ),
        sessions.create(
            "user-1",
            _NOW + IDLE_WINDOW,
            ip_address=_ADDRESS,
            user_agent=_AGENT,
        ),
    )

    ended = users.replace_password_and_end_sessions("user-1", _THE_NEW_HASH)

    assert ended == len(opened)
    account = users.get_by_username("felix")
    assert account is not None
    assert account.password_hash == _THE_NEW_HASH
    assert all(sessions.load(session.id) is None for session in opened)


def test_a_session_delete_a_trigger_refuses_leaves_the_old_hash_standing(
    database: Path,
) -> None:
    users = SqliteUserStore(database)
    users.add_first_account(an_account(password_hash=_THE_OLD_HASH))
    sessions = SqliteSessionRecordStore(database, clock=FrozenClock(instant=_NOW))
    kept = sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TRIGGER refuse_session_delete BEFORE DELETE ON sessions"
            " BEGIN SELECT RAISE(ABORT, 'a session may not be dropped here'); END;",
        )

    with pytest.raises(sqlite3.IntegrityError):
        users.replace_password_and_end_sessions("user-1", _THE_NEW_HASH)

    account = users.get_by_username("felix")
    assert account is not None
    assert account.password_hash == _THE_OLD_HASH
    assert sessions.load(kept.id) is not None


def test_an_older_file_gains_the_origin_columns(tmp_path: Path) -> None:
    database = tmp_path / "identity.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE login_attempts (
                username TEXT NOT NULL,
                failed_at TEXT NOT NULL
            );
            """,
        )

    create_identity_tables(database)
    clock = FrozenClock(instant=_NOW)
    sessions = SqliteSessionRecordStore(database, clock=clock)
    attempts = SqliteLoginAttemptStore(database, clock=clock)

    session = sessions.create(
        "user-1",
        _NOW + IDLE_WINDOW,
        ip_address=_ADDRESS,
        user_agent=_AGENT,
    )
    attempts.record(ip_address=_ADDRESS, username="felix", success=False)

    loaded = sessions.load(session.id)
    assert loaded is not None
    assert loaded.ip_address == _ADDRESS
    assert (
        attempts.count_recent_failures(
            ip_address=_ADDRESS,
            window_seconds=300,
            username="felix",
        )
        == 1
    )
