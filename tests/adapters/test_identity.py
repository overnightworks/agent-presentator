"""The identity bridge against a temporary SQLite file and real Argon2id."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from presentator.adapters.identity import (
    Argon2PasswordHasher,
    HmacSessionCookieSigner,
    SqliteLoginAttemptStore,
    SqliteSessionRecordStore,
    SqliteUserStore,
    SystemClock,
    create_identity_tables,
)
from presentator.contracts.models import (
    Credentials,
    FirstStartClosedError,
    Role,
    Session,
    User,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_INSTANCE_KEY = b"thirty-two-bytes-of-instance-key"
_TYPED_WORDS = "the words only this test types"
_STORED_HASH = "a hash of something else"


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


def test_an_account_is_read_back_by_name_with_the_hash_it_was_stored_with(
    database: Path,
) -> None:
    users = SqliteUserStore(database)
    hasher = Argon2PasswordHasher()
    users.add_first_account(
        Credentials(user=an_admin(), password_hash=hasher.hash(_TYPED_WORDS)),
    )

    credentials = users.credentials_for("felix")

    assert credentials is not None
    assert credentials.user == an_admin()
    assert hasher.verify(_TYPED_WORDS, credentials.password_hash) is True
    assert hasher.verify("guessed", credentials.password_hash) is False


def test_a_stored_password_is_not_the_password(database: Path) -> None:
    users = SqliteUserStore(database)
    hasher = Argon2PasswordHasher()
    users.add_first_account(
        Credentials(user=an_admin(), password_hash=hasher.hash(_TYPED_WORDS)),
    )

    credentials = users.credentials_for("felix")

    assert credentials is not None
    assert _TYPED_WORDS not in credentials.password_hash
    assert credentials.password_hash.startswith("$argon2id$")


def test_an_account_is_read_back_by_id_and_counted(database: Path) -> None:
    users = SqliteUserStore(database)
    assert users.count() == 0

    users.add_first_account(Credentials(user=an_admin(), password_hash=_STORED_HASH))

    assert users.get("user-1") == an_admin()
    assert users.count() == 1


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(SqliteUserStore.get, id="by-id"),
        pytest.param(SqliteUserStore.credentials_for, id="by-name"),
    ],
)
def test_an_account_that_does_not_exist_reads_as_nothing(
    database: Path,
    read: Callable[[SqliteUserStore, str], User | Credentials | None],
) -> None:
    assert read(SqliteUserStore(database), "nobody") is None


def test_a_touched_session_keeps_its_row_and_moves_its_last_seen(
    database: Path,
) -> None:
    sessions = SqliteSessionRecordStore(database)
    session = Session(id="session-1", user_id="user-1", last_seen=_NOW)
    sessions.put(session)

    later = _NOW + timedelta(minutes=15)
    sessions.put(Session(id="session-1", user_id="user-1", last_seen=later))

    stored = sessions.get("session-1")
    assert stored is not None
    assert stored.last_seen == later


def test_a_removed_session_is_gone(database: Path) -> None:
    sessions = SqliteSessionRecordStore(database)
    sessions.put(Session(id="session-1", user_id="user-1", last_seen=_NOW))

    sessions.remove("session-1")

    assert sessions.get("session-1") is None


def test_failed_attempts_are_counted_only_inside_the_window(database: Path) -> None:
    attempts = SqliteLoginAttemptStore(database)
    within_the_hour = (_NOW - timedelta(hours=1), _NOW)
    for at in within_the_hour:
        attempts.record_failure("felix", at=at)
    attempts.record_failure("someone-else", at=_NOW)

    assert attempts.failure_count("felix", since=_NOW - timedelta(minutes=5)) == 1
    assert attempts.failure_count(
        "felix",
        since=_NOW - timedelta(days=1),
    ) == len(within_the_hour)


def test_a_cookie_this_instance_signed_names_its_session() -> None:
    signer = HmacSessionCookieSigner(_INSTANCE_KEY)

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
    signer = HmacSessionCookieSigner(_INSTANCE_KEY)
    tampered = cookie.format(signature=signer.sign("session-1").split(".")[1])

    assert signer.session_id_from(tampered) is None


def test_another_instance_key_refuses_the_cookie() -> None:
    cookie = HmacSessionCookieSigner(_INSTANCE_KEY).sign("session-1")

    assert (
        HmacSessionCookieSigner(b"another-instances-thirty-two-key").session_id_from(
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
    users.add_first_account(
        Credentials(user=an_admin(), password_hash=_STORED_HASH),
    )

    with pytest.raises(FirstStartClosedError, match="already has an account"):
        users.add_first_account(
            Credentials(
                user=an_admin(user_id="user-2", username="stranger"),
                password_hash=_STORED_HASH,
            ),
        )

    assert users.count() == 1
    assert users.credentials_for("stranger") is None


def test_a_password_checked_against_no_account_is_refused() -> None:
    assert Argon2PasswordHasher().verify(_TYPED_WORDS, None) is False
