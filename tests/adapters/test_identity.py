"""The identity bridge against a temporary SQLite file."""

import sqlite3
from collections.abc import Callable
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
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    StoredSession,
    SystemClock,
    create_identity_tables,
)
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Role, User
from presentator.host.main import web_auth_config_for
from tests.people import (
    copy_of_a_crashed_database,
    seed_user,
    write_password_schema,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_ID_MARKER = "id-MARKER-4f7c1e"
_A_FIRST_SESSIONS_TABLE_WITH_ITS_ROWS = (
    "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL"
    " UNIQUE, role TEXT NOT NULL);"
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL"
    " REFERENCES users(id), created_at TEXT NOT NULL, expires_at TEXT NOT NULL);"
    "CREATE TABLE decks (slug TEXT PRIMARY KEY);"
    "INSERT INTO users VALUES ('user-1', 'felix', 'admin');"
    "INSERT INTO sessions VALUES ('s-1', 'user-1', 'then', 'later');"
    "INSERT INTO decks VALUES ('talk');"
)
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
    return SqliteSessionRecordStore(database, keeps_id_token=True)


def an_id_token(word: str = _ID_MARKER) -> SecretStr:
    return SecretStr(word)


def open_session(
    store: SqliteSessionRecordStore,
    user_id: str = "user-1",
    *,
    now: datetime = _NOW,
    expires_at: datetime = _NOW,
    refresh_token: SecretStr | None = None,
) -> StoredSession:
    return store.create(
        user_id,
        now=now,
        expires_at=expires_at,
        refresh_token=refresh_token,
        id_token=an_id_token(),
    )


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

    created = open_session(sessions, "user-1", expires_at=expires_at)

    loaded = sessions.load(created.id)
    assert loaded is not None
    assert loaded.user == an_admin()
    assert (loaded.created_at, loaded.expires_at) == (_NOW, expires_at)


def test_a_session_for_an_account_that_does_not_exist_is_refused_loudly(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    with pytest.raises(LookupError, match="nobody"):
        open_session(sessions, "nobody")

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
    session = open_session(sessions, "user-1")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DELETE FROM users WHERE id = 'user-1'")

    assert sessions.load(session.id) is None


def test_a_touched_session_keeps_its_creation_and_takes_the_given_expiry(
    sessions: SqliteSessionRecordStore,
) -> None:
    session = open_session(sessions, "user-1")
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
    session = open_session(sessions, "user-1")

    assert sessions.load(session.id) is not None
    logout.delete(session.id)
    renewed = sessions.touch(session.id, expires_at=_NOW + timedelta(minutes=1))

    assert renewed is False
    assert sessions.load(session.id) is None


def test_a_removed_session_is_gone(sessions: SqliteSessionRecordStore) -> None:
    session = open_session(sessions, "user-1")

    sessions.delete(session.id)

    assert sessions.load(session.id) is None


def test_deleting_one_persons_sessions_counts_them_and_leaves_another_standing(
    sessions: SqliteSessionRecordStore,
) -> None:
    kept = open_session(sessions, "user-2")
    open_session(sessions, "user-1")

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
        open_session(sessions, "user-1", now=_NOW + timedelta(minutes=minute))
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
        refresh_token=None,
        id_token=an_id_token(),
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
    session = open_session(sessions, "user-1", expires_at=_NOW + timedelta(hours=1))
    at = _NOW + timedelta(minutes=30)

    admitted = admit_session(session.id, sessions=sessions, config=a_config_reading(at))

    assert not isinstance(admitted, ConnectionRefusal)
    assert admitted.username == "felix"
    assert stored_expiry(database, session.id) == (_NOW + SESSION_LIFETIME).isoformat()


@pytest.mark.parametrize(
    ("idle_expiry", "refused_at"),
    [
        pytest.param(
            timedelta(hours=1), timedelta(hours=1, seconds=1), id="past-its-expiry"
        ),
        pytest.param(
            SESSION_LIFETIME - timedelta(minutes=1),
            SESSION_LIFETIME + timedelta(seconds=1),
            id="past-the-cap",
        ),
    ],
)
def test_a_refused_session_is_not_renewed(
    database: Path,
    sessions: SqliteSessionRecordStore,
    idle_expiry: timedelta,
    refused_at: timedelta,
) -> None:
    session = open_session(sessions, "user-1", expires_at=_NOW + idle_expiry)
    expiry_before = stored_expiry(database, session.id)

    refused = admit_session(
        session.id, sessions=sessions, config=a_config_reading(_NOW + refused_at)
    )

    assert refused is ConnectionRefusal.SESSION_ENDED_OR_EXPIRED
    assert stored_expiry(database, session.id) == expiry_before


def a_config_reading(now: datetime) -> WebAuthConfig:
    return replace(
        web_auth_config_for(
            SecretStr("t" * 32),
            trusted_proxies="",
            origin="https://testserver",
        ),
        wall_clock=now.timestamp,
    )


def test_creating_the_tables_twice_leaves_the_people_as_they_were(
    database: Path,
) -> None:
    session = SqliteSessionRecordStore(database).create(
        "user-1", now=_NOW, expires_at=_NOW, refresh_token=None, id_token=an_id_token()
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
    assert session_columns == [
        "id",
        "user_id",
        "created_at",
        "expires_at",
        "refresh_token",
        "id_token",
    ]
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
        pytest.param(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL"
            " UNIQUE, role TEXT NOT NULL);"
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL"
            " REFERENCES users(id), created_at TEXT NOT NULL, expires_at TEXT NOT"
            " NULL, refresh_token TEXT);",
            id="sessions-before-the-id-token-column",
        ),
        pytest.param(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL,"
            " role TEXT NOT NULL, password_hash TEXT NOT NULL);"
            "CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " url TEXT NOT NULL, credential_reference TEXT, encrypted_secret BLOB);"
            "INSERT INTO sources (id, name, url, credential_reference)"
            " VALUES ('s', 'talks', 'https://example.test/talks.git', 'TOKEN');",
            id="before-encryption-a-source-seeded-from-the-environment",
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

    with pytest.raises(OldDatabaseError, match=r"start-over-from-an-old-database\.md"):
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
    assert columns == [
        "id",
        "user_id",
        "created_at",
        "expires_at",
        "refresh_token",
        "id_token",
    ]


def test_a_password_schema_only_in_the_write_ahead_log_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    database = copy_of_a_crashed_database(
        tmp_path / "crashed" / "identity.sqlite3", write_password_schema
    )
    companions = [database.with_name(database.name + "-wal"), database]
    bytes_before = [path.read_bytes() for path in companions]
    folder_before = set(database.parent.iterdir())

    with pytest.raises(OldDatabaseError, match=r"start-over-from-an-old-database\.md"):
        create_identity_tables(database)

    assert [path.read_bytes() for path in companions] == bytes_before
    assert set(database.parent.iterdir()) - folder_before <= {
        database.with_name(database.name + "-shm")
    }


@pytest.mark.parametrize(
    ("expires_at", "stays"),
    [
        (_NOW - timedelta(seconds=1), False),
        (_NOW, True),
        (_NOW + timedelta(seconds=1), True),
    ],
    ids=["before the sweep", "at the sweep", "after the sweep"],
)
def test_a_sweep_deletes_a_session_only_when_it_ended_before_the_sweep(
    sessions: SqliteSessionRecordStore,
    expires_at: datetime,
    *,
    stays: bool,
) -> None:
    session = open_session(
        sessions, "user-1", now=_NOW - SESSION_LIFETIME, expires_at=expires_at
    )

    deleted = sessions.delete_expired(_NOW)

    assert (sessions.load(session.id) is not None) is stays
    assert deleted == (0 if stays else 1)


@pytest.mark.parametrize(
    "after_expiry",
    [timedelta(0), timedelta(milliseconds=500), timedelta(seconds=1)],
    ids=["at the expiry", "within the second after", "a second after"],
)
def test_the_sweep_keeps_exactly_the_sessions_the_admission_admits(
    sessions: SqliteSessionRecordStore,
    after_expiry: timedelta,
) -> None:
    expires_at = _NOW + timedelta(hours=1)
    admitted_one = open_session(sessions, "user-1", expires_at=expires_at)
    swept_one = open_session(sessions, "user-2", expires_at=expires_at)
    at = expires_at + after_expiry

    admitted = admit_session(
        admitted_one.id, sessions=sessions, config=a_config_reading(at)
    )
    sessions.delete_expired(at)

    assert (not isinstance(admitted, ConnectionRefusal)) is (
        sessions.load(swept_one.id) is not None
    )


def dump_without_sessions(database: Path) -> list[str]:
    with closing(sqlite3.connect(database)) as connection:
        return [
            statement
            for statement in connection.iterdump()
            if not statement.startswith('INSERT INTO "sessions"')
        ]


def test_a_sweep_leaves_the_accounts_and_every_other_table_as_they_were(
    database: Path,
    sessions: SqliteSessionRecordStore,
) -> None:
    ended = _NOW - SESSION_LIFETIME
    open_session(sessions, "user-1", now=ended, expires_at=ended)
    before = dump_without_sessions(database)

    deleted = sessions.delete_expired(_NOW)

    assert deleted == 1
    assert any("felix" in statement for statement in before)
    assert dump_without_sessions(database) == before


def a_token(text: str = "rt-MARKER") -> SecretStr:
    return SecretStr(text)


def occurrences_in_the_file(database: Path, text: str) -> int:
    """How often `text` appears in the dump of every table of the file."""
    with closing(sqlite3.connect(database)) as connection:
        return "\n".join(connection.iterdump()).count(text)


def test_a_refresh_token_is_stored_in_the_row_and_read_back_as_a_secret(
    database: Path, sessions: SqliteSessionRecordStore
) -> None:
    created = open_session(sessions, "user-1", refresh_token=a_token())

    loaded = sessions.load(created.id)

    assert loaded is not None
    assert loaded.refresh_token is not None
    assert loaded.refresh_token.get_secret_value() == "rt-MARKER"
    assert occurrences_in_the_file(database, "rt-MARKER") == 1


def test_a_session_without_a_refresh_token_reads_back_none(
    sessions: SqliteSessionRecordStore,
) -> None:
    created = open_session(sessions, "user-1")

    loaded = sessions.load(created.id)

    assert loaded is not None
    assert loaded.refresh_token is None


def test_a_replaced_refresh_token_is_read_back_and_the_answer_is_true(
    sessions: SqliteSessionRecordStore,
) -> None:
    created = open_session(sessions, "user-1", refresh_token=a_token())

    other = open_session(sessions, "user-1", refresh_token=a_token("rt-other"))

    answer = sessions.replace_refresh_token(created.id, refresh_token=a_token("rt-2"))

    loaded = sessions.load(created.id)
    untouched = sessions.load(other.id)
    assert answer is True
    assert loaded is not None
    assert loaded.refresh_token == a_token("rt-2")
    assert untouched is not None
    assert untouched.refresh_token == a_token("rt-other")


@pytest.mark.parametrize("session_id", ["never-existed", "deleted"])
def test_replacing_the_token_of_a_gone_session_answers_false_and_creates_no_row(
    database: Path, sessions: SqliteSessionRecordStore, session_id: str
) -> None:
    created = open_session(sessions, "user-1")
    sessions.delete(created.id)
    target = created.id if session_id == "deleted" else session_id

    answer = sessions.replace_refresh_token(target, refresh_token=a_token())

    assert answer is False
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)


def test_a_logout_on_another_store_before_a_replacement_leaves_the_row_gone(
    database: Path, sessions: SqliteSessionRecordStore
) -> None:
    created = open_session(sessions, "user-1", refresh_token=a_token())
    SqliteSessionRecordStore(database).delete(created.id)

    answer = sessions.replace_refresh_token(created.id, refresh_token=a_token("rt-2"))

    assert answer is False
    assert sessions.load(created.id) is None
    assert occurrences_in_the_file(database, "rt-2") == 0


def end_by_delete(store: SqliteSessionRecordStore, session_id: str) -> None:
    store.delete(session_id)


def end_by_delete_for_user(store: SqliteSessionRecordStore, session_id: str) -> None:
    del session_id
    store.delete_for_user("user-1")


def end_by_delete_expired(store: SqliteSessionRecordStore, session_id: str) -> None:
    del session_id
    store.delete_expired(_NOW + timedelta(days=1))


def end_by_prune_overflow(store: SqliteSessionRecordStore, session_id: str) -> None:
    del session_id
    store.prune_overflow("user-1", 0)


def end_by_delete_account(store: SqliteSessionRecordStore, session_id: str) -> None:
    del session_id
    SqliteSubjectUserStore(store.database, ProcessWriteLock()).delete_account("user-1")


@pytest.mark.parametrize(
    "remove",
    [
        end_by_delete,
        end_by_delete_for_user,
        end_by_delete_expired,
        end_by_prune_overflow,
        end_by_delete_account,
    ],
)
def test_every_way_of_ending_a_session_removes_its_token_with_the_row(
    database: Path,
    sessions: SqliteSessionRecordStore,
    remove: Callable[[SqliteSessionRecordStore, str], None],
) -> None:
    created = open_session(sessions, "user-1", refresh_token=a_token())
    assert occurrences_in_the_file(database, "rt-MARKER") == 1
    assert occurrences_in_the_file(database, _ID_MARKER) == 1

    remove(sessions, created.id)

    assert occurrences_in_the_file(database, "rt-MARKER") == 0
    assert occurrences_in_the_file(database, _ID_MARKER) == 0
    with closing(sqlite3.connect(database)) as connection:
        owners = [
            table
            for (table,) in connection.execute(
                "SELECT m.name FROM sqlite_master m,"
                " pragma_table_info(m.name) c WHERE m.type = 'table'"
                " AND c.name = 'refresh_token'"
            )
        ]
    assert owners == ["sessions"]


def test_a_session_never_shows_its_tokens_in_repr_or_str(
    sessions: SqliteSessionRecordStore,
) -> None:
    """#428 cell 4: neither `repr` nor `str` shows a token."""
    created = sessions.create(
        "user-1",
        now=_NOW,
        expires_at=_NOW,
        refresh_token=a_token(),
        id_token=an_id_token("id-MARKER"),
    )

    for shown in (repr(created), str(created), repr(sessions.load(created.id))):
        assert "rt-MARKER" not in shown
        assert "id-MARKER" not in shown


def test_the_id_token_is_read_back_and_survives_a_touch_and_a_refresh_token_swap(
    sessions: SqliteSessionRecordStore,
) -> None:
    """#428 cells 1-3: the ID token reads back and outlives a touch and a swap."""
    created = sessions.create(
        "user-1",
        now=_NOW,
        expires_at=_NOW,
        refresh_token=a_token(),
        id_token=an_id_token("id-kept"),
    )

    sessions.touch(created.id, expires_at=_NOW + timedelta(hours=1))
    sessions.replace_refresh_token(created.id, refresh_token=a_token("rt-2"))

    loaded = sessions.load(created.id)
    assert loaded is not None
    assert loaded.id_token is not None
    assert loaded.id_token.get_secret_value() == "id-kept"


def test_a_store_that_does_not_keep_the_id_token_writes_none(
    database: Path,
) -> None:
    """#428 D4 (T1): a store that does not keep the ID token writes NULL."""
    store = SqliteSessionRecordStore(database)
    created = store.create(
        "user-1",
        now=_NOW,
        expires_at=_NOW,
        refresh_token=None,
        id_token=an_id_token("id-dropped"),
    )

    assert created.id_token is None
    loaded = store.load(created.id)
    assert loaded is not None
    assert loaded.id_token is None
    assert occurrences_in_the_file(database, "id-dropped") == 0


def write_the_first_sessions_table_in_the_log(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(_A_FIRST_SESSIONS_TABLE_WITH_ITS_ROWS)


def test_a_file_with_the_first_sessions_table_is_refused_and_left_byte_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "identity.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "PRAGMA journal_mode=WAL;" + _A_FIRST_SESSIONS_TABLE_WITH_ITS_ROWS
        )
    before = database.read_bytes()

    with pytest.raises(OldDatabaseError, match=r"start-over-from-an-old-database\.md"):
        create_identity_tables(database)

    assert database.read_bytes() == before


def test_the_first_sessions_table_committed_only_in_the_log_is_refused_untouched(
    tmp_path: Path,
) -> None:
    database = copy_of_a_crashed_database(
        tmp_path / "crashed" / "identity.sqlite3",
        write_the_first_sessions_table_in_the_log,
    )
    companions = [database.with_name(database.name + "-wal"), database]
    bytes_before = [path.read_bytes() for path in companions]

    with pytest.raises(OldDatabaseError, match=r"start-over-from-an-old-database\.md"):
        create_identity_tables(database)

    assert [path.read_bytes() for path in companions] == bytes_before
