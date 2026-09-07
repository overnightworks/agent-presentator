"""The identity bridge against a temporary SQLite file and real Argon2id."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webauth.liveness import IdleWindowLiveness

from presentator.adapters.identity import (
    Argon2PasswordHasher,
    IdleWindow,
    SignedSessionCookie,
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
_INSTANCE_KEY = b"thirty-two-bytes-of-instance-key"
_TYPED_WORDS = "the words only this test types"
_STORED_HASH = "a hash of something else"
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
    hasher = Argon2PasswordHasher()
    users.add_first_account(
        an_account(password_hash=hasher.hash(_TYPED_WORDS)),
    )

    account = users.get_by_username("felix")

    assert account is not None
    assert account.as_user() == an_admin()
    assert hasher.verify(_TYPED_WORDS, account.password_hash) is True
    assert hasher.verify("guessed", account.password_hash) is False


def test_a_stored_password_is_not_the_password(database: Path) -> None:
    users = SqliteUserStore(database)
    hasher = Argon2PasswordHasher()
    users.add_first_account(
        an_account(password_hash=hasher.hash(_TYPED_WORDS)),
    )

    account = users.get_by_username("felix")

    assert account is not None
    assert _TYPED_WORDS not in account.password_hash
    assert account.password_hash.startswith("$argon2id$")


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
    sessions.touch(
        session,
        ip_address="198.51.100.8",
        user_agent="LaterBrowser/2.0",
        now=later,
    )

    stored = sessions.load(session.id)
    assert stored is not None
    assert stored.last_seen == later
    assert stored.ip_address == "198.51.100.8"
    assert stored.user_agent == "LaterBrowser/2.0"


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


def test_a_cookie_this_instance_signed_names_its_session() -> None:
    signer = SignedSessionCookie(_INSTANCE_KEY)

    cookie = signer.sign("session-1")

    assert "session-1" in cookie
    assert signer.session_id_from(cookie) == "session-1"


@pytest.mark.parametrize(
    "cookie",
    [
        pytest.param("session-1", id="no-signature"),
        pytest.param("session-1.00", id="wrong-signature"),
        pytest.param("session-2.{signature}", id="signature-of-another-session"),
    ],
)
def test_a_cookie_that_was_not_signed_here_names_nothing(cookie: str) -> None:
    signer = SignedSessionCookie(_INSTANCE_KEY)
    tampered = cookie.format(signature=signer.sign("session-1").split(".")[1])

    assert signer.session_id_from(tampered) is None


def test_another_instance_key_refuses_the_cookie() -> None:
    cookie = SignedSessionCookie(_INSTANCE_KEY).sign("session-1")

    assert (
        SignedSessionCookie(b"another-instances-thirty-two-key").session_id_from(
            cookie,
        )
        is None
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


def test_a_password_checked_against_no_account_is_refused() -> None:
    assert Argon2PasswordHasher().verify(_TYPED_WORDS, None) is False


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
