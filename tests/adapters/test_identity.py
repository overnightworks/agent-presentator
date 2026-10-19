"""The identity bridge against a temporary SQLite file."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webauth.liveness import IdleWindowLiveness

from presentator.adapters.identity import (
    IdleWindow,
    PasswordDatabaseError,
    SqliteSessionRecordStore,
    SqliteUserStore,
    SystemClock,
    create_identity_tables,
)
from presentator.application.identity import IDLE_WINDOW
from presentator.contracts.models import Role, Session, User
from tests.application.fakes import FrozenClock
from tests.people import seed_user

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_ADDRESS = "203.0.113.7"
_AGENT = "TestBrowser/1.0"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    return database


def an_admin(user_id: str = "user-1", username: str = "felix") -> User:
    return User(id=user_id, username=username, role=Role.ADMIN)


def test_the_database_keeps_its_write_ahead_log(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"


def test_a_person_is_read_back_by_id(database: Path) -> None:
    seed_user(database, an_admin())

    assert SqliteUserStore(database).get("user-1") == an_admin()


def test_a_person_that_does_not_exist_reads_as_nothing(database: Path) -> None:
    assert SqliteUserStore(database).get("nobody") is None


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


def test_the_system_clock_reads_an_aware_utc_time() -> None:
    assert SystemClock().now().tzinfo == UTC


def test_the_idle_window_admits_a_session_at_the_boundary() -> None:
    session = Session(id="session-1", user_id="user-1", last_seen=_NOW)
    window = IdleWindow(IdleWindowLiveness(int(IDLE_WINDOW.total_seconds())))

    assert window.admits(session, at=_NOW + IDLE_WINDOW) is True
    assert window.admits(session, at=_NOW + IDLE_WINDOW + timedelta(seconds=1)) is False


def test_creating_the_tables_twice_leaves_the_people_as_they_were(
    database: Path,
) -> None:
    seed_user(database, an_admin())

    create_identity_tables(database)

    assert SqliteUserStore(database).get("user-1") == an_admin()


def test_a_fresh_database_stores_no_password_and_no_login_attempts(
    database: Path,
) -> None:
    with sqlite3.connect(database) as connection:
        user_columns = [
            column[1] for column in connection.execute("PRAGMA table_info(users)")
        ]
        tables = {
            table[0]
            for table in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }

    assert user_columns == ["id", "username", "role"]
    assert "login_attempts" not in tables


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


@pytest.mark.parametrize(
    "password_version",
    [
        pytest.param(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL,"
            " role TEXT NOT NULL, password_hash TEXT NOT NULL);",
            id="password-hash-column",
        ),
        pytest.param(
            "CREATE TABLE login_attempts (username TEXT NOT NULL,"
            " failed_at TEXT NOT NULL);",
            id="login-attempts-table",
        ),
    ],
)
def test_a_file_from_the_password_version_is_refused_and_left_untouched(
    tmp_path: Path,
    password_version: str,
) -> None:
    database = tmp_path / "identity.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("PRAGMA journal_mode=WAL;" + password_version)
    before = database.read_bytes()
    folder_before = sorted(tmp_path.iterdir())

    with pytest.raises(PasswordDatabaseError, match=r"OPERATIONS\.md"):
        create_identity_tables(database)

    assert database.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == folder_before
