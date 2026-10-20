"""The identity bridge against a temporary SQLite file."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr
from webauth.config import WebAuthConfig
from webauth.dependencies import ConnectionRefusal, admit_session

from presentator.adapters.identity import (
    OldDatabaseError,
    SqliteSessionRecordStore,
    SystemClock,
    create_identity_tables,
)
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Role, User
from presentator.host.main import lobby_web_auth_config
from tests.people import (
    copy_of_a_crashed_database,
    seed_user,
    write_password_schema,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_A_SESSIONS_TABLE_OF_THE_PREVIOUS_VERSION = (
    "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL"
    " UNIQUE, role TEXT NOT NULL);"
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL"
    " REFERENCES users(id), created_at TEXT NOT NULL, last_seen TEXT NOT NULL,"
    " ip_address TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '');"
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    seed_user(database, an_admin())
    seed_user(database, an_admin("user-2", "anna"))
    return database


@pytest.fixture
def sessions(database: Path) -> SqliteSessionRecordStore:
    return SqliteSessionRecordStore(database)


def an_admin(user_id: str = "user-1", username: str = "felix") -> User:
    return User(id=user_id, username=username, role=Role.ADMIN)


def stored_expiry(database: Path, session_id: str) -> str:
    with closing(sqlite3.connect(database)) as connection:
        (expires_at,) = connection.execute(
            "SELECT expires_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return str(expires_at)


def test_the_database_keeps_its_write_ahead_log(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"


def test_a_session_is_read_back_with_the_account_joined_from_the_users(
    sessions: SqliteSessionRecordStore,
) -> None:
    expires_at = _NOW + timedelta(hours=1)

    created = sessions.create("user-1", now=_NOW, expires_at=expires_at)

    loaded = sessions.load(created.id)
    assert loaded is not None
    assert loaded.user == an_admin()
    assert (loaded.created_at, loaded.expires_at) == (_NOW, expires_at)


def test_a_session_for_an_account_that_does_not_exist_is_refused_loudly(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    with pytest.raises(LookupError, match="nobody"):
        sessions.create("nobody", now=_NOW, expires_at=_NOW)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)


def test_a_session_nobody_created_reads_as_nothing(
    sessions: SqliteSessionRecordStore,
) -> None:
    assert sessions.load("nobody") is None


def test_a_session_whose_account_row_is_gone_reads_as_nothing(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    session = sessions.create("user-1", now=_NOW, expires_at=_NOW)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DELETE FROM users WHERE id = 'user-1'")

    assert sessions.load(session.id) is None


def test_a_touched_session_keeps_its_creation_and_takes_the_given_expiry(
    sessions: SqliteSessionRecordStore,
) -> None:
    session = sessions.create("user-1", now=_NOW, expires_at=_NOW)
    later = _NOW + timedelta(minutes=15)

    renewed = sessions.touch(session.id, expires_at=later)

    stored = sessions.load(session.id)
    assert renewed is True
    assert stored is not None
    assert (stored.created_at, stored.expires_at) == (_NOW, later)


def test_a_session_deleted_after_its_load_is_not_brought_back_by_touch(
    sessions: SqliteSessionRecordStore,
    database: Path,
) -> None:
    logout = SqliteSessionRecordStore(database)
    session = sessions.create("user-1", now=_NOW, expires_at=_NOW)

    assert sessions.load(session.id) is not None
    logout.delete(session.id)
    renewed = sessions.touch(session.id, expires_at=_NOW + timedelta(minutes=1))

    assert renewed is False
    assert sessions.load(session.id) is None


def test_a_removed_session_is_gone(sessions: SqliteSessionRecordStore) -> None:
    session = sessions.create("user-1", now=_NOW, expires_at=_NOW)

    sessions.delete(session.id)

    assert sessions.load(session.id) is None


def test_deleting_one_persons_sessions_counts_them_and_leaves_another_standing(
    sessions: SqliteSessionRecordStore,
) -> None:
    kept = sessions.create("user-2", now=_NOW, expires_at=_NOW)
    sessions.create("user-1", now=_NOW, expires_at=_NOW)

    dropped = sessions.delete_for_user("user-1")

    assert dropped == 1
    assert sessions.load(kept.id) is not None


def test_deleting_sessions_for_nobody_drops_nothing(
    sessions: SqliteSessionRecordStore,
) -> None:
    assert sessions.delete_for_user("nobody") == 0


@pytest.mark.parametrize(
    ("keep_newest", "kept_of_oldest_middle_newest"),
    [
        pytest.param(2, (False, True, True), id="the-oldest-above-the-cap-goes"),
        pytest.param(5, (True, True, True), id="below-the-cap-drops-nothing"),
        pytest.param(0, (False, False, False), id="zero-keeps-none"),
    ],
)
def test_overflow_sessions_drop_the_oldest_by_creation_and_keep_the_newest(
    sessions: SqliteSessionRecordStore,
    keep_newest: int,
    kept_of_oldest_middle_newest: tuple[bool, bool, bool],
) -> None:
    created = [
        sessions.create("user-1", now=_NOW + timedelta(minutes=minute), expires_at=_NOW)
        for minute in range(3)
    ]

    sessions.prune_overflow("user-1", keep_newest=keep_newest)

    assert tuple(sessions.load(session.id) is not None for session in created) == (
        kept_of_oldest_middle_newest
    )


def test_a_timestamp_given_with_an_offset_is_stored_as_the_same_instant_in_utc(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    in_berlin = timezone(timedelta(hours=1))

    session = sessions.create(
        "user-1",
        now=_NOW.astimezone(in_berlin),
        expires_at=(_NOW + timedelta(hours=1)).astimezone(in_berlin),
    )

    assert stored_expiry(database, session.id) == "2026-01-15T10:00:00+00:00"
    loaded = sessions.load(session.id)
    assert loaded is not None
    assert loaded.created_at == _NOW


def test_the_system_clock_reads_an_aware_utc_time() -> None:
    assert SystemClock().now().tzinfo == UTC


def test_an_admitted_session_is_renewed_to_the_cap_when_the_window_reaches_it(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    session = sessions.create("user-1", now=_NOW, expires_at=_NOW + timedelta(hours=1))
    at = _NOW + timedelta(minutes=30)

    admitted = admit_session(session.id, sessions=sessions, config=a_config_reading(at))

    assert not isinstance(admitted, ConnectionRefusal)
    assert admitted.username == "felix"
    assert stored_expiry(database, session.id) == (_NOW + SESSION_LIFETIME).isoformat()


def test_a_refused_session_is_not_renewed(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    session = sessions.create("user-1", now=_NOW, expires_at=_NOW + SESSION_LIFETIME)
    expiry_before = stored_expiry(database, session.id)
    past_the_cap = _NOW + SESSION_LIFETIME + timedelta(seconds=1)

    refused = admit_session(
        session.id, sessions=sessions, config=a_config_reading(past_the_cap)
    )

    assert refused is ConnectionRefusal.SESSION_ENDED_OR_EXPIRED
    assert stored_expiry(database, session.id) == expiry_before


def a_config_reading(now: datetime) -> WebAuthConfig:
    return replace(
        lobby_web_auth_config(
            SecretStr("t" * 32),
            trusted_proxies="",
            public_origin="https://testserver",
        ),
        wall_clock=now.timestamp,
    )


def test_creating_the_tables_twice_leaves_the_people_as_they_were(
    database: Path,
) -> None:
    session = SqliteSessionRecordStore(database).create(
        "user-1", now=_NOW, expires_at=_NOW
    )

    create_identity_tables(database)

    loaded = SqliteSessionRecordStore(database).load(session.id)
    assert loaded is not None
    assert loaded.user == an_admin()


def test_a_fresh_database_stores_no_password_no_login_attempts_and_no_client_address(
    database: Path,
) -> None:
    with sqlite3.connect(database) as connection:
        user_columns = [
            column[1] for column in connection.execute("PRAGMA table_info(users)")
        ]
        session_columns = [
            column[1] for column in connection.execute("PRAGMA table_info(sessions)")
        ]
        tables = {
            table[0]
            for table in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }

    assert user_columns == ["id", "username", "role"]
    assert session_columns == ["id", "user_id", "created_at", "expires_at"]
    assert "login_attempts" not in tables


@pytest.mark.parametrize(
    "older_version",
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
        pytest.param(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL"
            " UNIQUE, role TEXT NOT NULL);"
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL"
            " REFERENCES users(id), last_seen TEXT NOT NULL);",
            id="sessions-without-created-at",
        ),
        pytest.param(
            _A_SESSIONS_TABLE_OF_THE_PREVIOUS_VERSION,
            id="sessions-with-last-seen-and-a-client-address",
        ),
    ],
)
def test_a_file_from_an_older_version_is_refused_and_left_untouched(
    tmp_path: Path,
    older_version: str,
) -> None:
    database = tmp_path / "identity.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("PRAGMA journal_mode=WAL;" + older_version)
    before = database.read_bytes()
    folder_before = sorted(tmp_path.iterdir())

    with pytest.raises(OldDatabaseError, match=r"OPERATIONS\.md"):
        create_identity_tables(database)

    assert database.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == folder_before


@pytest.mark.parametrize(
    "started",
    [
        pytest.param("", id="file-with-no-tables"),
        pytest.param(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL"
            " UNIQUE, role TEXT NOT NULL);",
            id="users-only-after-a-first-start-stopped-early",
        ),
    ],
)
def test_a_file_this_version_started_is_completed_to_sessions_without_a_client_address(
    tmp_path: Path,
    started: str,
) -> None:
    database = tmp_path / "identity.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("PRAGMA journal_mode=WAL;" + started)

    create_identity_tables(database)

    with closing(sqlite3.connect(database)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(sessions)")]
    assert columns == ["id", "user_id", "created_at", "expires_at"]


def test_a_password_schema_only_in_the_write_ahead_log_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    database = copy_of_a_crashed_database(
        tmp_path / "crashed" / "identity.sqlite3", write_password_schema
    )
    companions = [database.with_name(database.name + "-wal"), database]
    bytes_before = [path.read_bytes() for path in companions]
    folder_before = set(database.parent.iterdir())

    with pytest.raises(OldDatabaseError, match=r"OPERATIONS\.md"):
        create_identity_tables(database)

    assert [path.read_bytes() for path in companions] == bytes_before
    assert set(database.parent.iterdir()) - folder_before <= {
        database.with_name(database.name + "-shm")
    }
