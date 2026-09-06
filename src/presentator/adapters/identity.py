"""SQLite rows, Argon2id hashes, and signed cookies behind the identity ports.

A bridge until `webauth` is tagged (ADR 0003): songmaker #835 brings the stores
and #833 the user management, and this module is deleted with them.
"""

import hmac
import secrets
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from presentator.contracts.models import Credentials, Role, Session, User

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    username TEXT NOT NULL,
    failed_at TEXT NOT NULL
);
"""
_IDENTIFIER_BYTES: Final = 32
_COOKIE_SEPARATOR: Final = "."


@contextmanager
def _rows(database: Path) -> Generator[sqlite3.Cursor]:
    connection = sqlite3.connect(database)
    try:
        yield connection.cursor()
        connection.commit()
    finally:
        connection.close()


def create_identity_tables(database: Path) -> None:
    """Make the identity schema exist; WAL is set once and stays in the file."""
    with _rows(database) as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.executescript(_SCHEMA)


@dataclass(frozen=True, slots=True)
class SqliteUserStore:
    """The accounts table."""

    database: Path

    def get(self, user_id: str) -> User | None:
        """Read the account a live session belongs to."""
        with _rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, username, role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return None if row is None else _user(row)

    def credentials_for(self, username: str) -> Credentials | None:
        """Read the account and its hash for the name someone typed."""
        with _rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, username, role, password_hash FROM users"
                " WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        *account, password_hash = row
        return Credentials(user=_user(account), password_hash=password_hash)

    def put(self, credentials: Credentials) -> None:
        """Write a new account; a name taken twice is a loud integrity error."""
        with _rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO users (id, username, role, password_hash)"
                " VALUES (?, ?, ?, ?)",
                (
                    credentials.user.id,
                    credentials.user.username,
                    credentials.user.role.value,
                    credentials.password_hash,
                ),
            )

    def count(self) -> int:
        """How many accounts exist, which is what first start asks."""
        with _rows(self.database) as cursor:
            (accounts,) = cursor.execute("SELECT count(*) FROM users").fetchone()
        return int(accounts)


@dataclass(frozen=True, slots=True)
class SqliteSessionRecordStore:
    """The sessions table."""

    database: Path

    def get(self, session_id: str) -> Session | None:
        """Read the row a cookie points at."""
        with _rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, user_id, last_seen FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        stored_id, user_id, last_seen = row
        return Session(
            id=stored_id,
            user_id=user_id,
            last_seen=datetime.fromisoformat(last_seen),
        )

    def put(self, session: Session) -> None:
        """Write the session, whether it is new or has just been touched."""
        with _rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO sessions (id, user_id, last_seen) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen",
                (session.id, session.user_id, session.last_seen.isoformat()),
            )

    def remove(self, session_id: str) -> None:
        """Delete the row, so logging out cannot be undone with the old cookie."""
        with _rows(self.database) as cursor:
            cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


@dataclass(frozen=True, slots=True)
class SqliteLoginAttemptStore:
    """The failed-attempts table."""

    database: Path

    def record_failure(self, username: str, *, at: datetime) -> None:
        """Remember one refused attempt for the name that was typed."""
        with _rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO login_attempts (username, failed_at) VALUES (?, ?)",
                (username, at.isoformat()),
            )

    def failure_count(self, username: str, *, since: datetime) -> int:
        """Count the refused attempts inside the caller's window."""
        with _rows(self.database) as cursor:
            (failures,) = cursor.execute(
                "SELECT count(*) FROM login_attempts"
                " WHERE username = ? AND failed_at >= ?",
                (username, since.isoformat()),
            ).fetchone()
        return int(failures)


@dataclass(frozen=True, slots=True)
class Argon2PasswordHasher:
    """Argon2id, named rather than taken from a recommendation that may move."""

    hashes: PasswordHash = field(
        default_factory=lambda: PasswordHash((Argon2Hasher(),)),
    )

    def hash(self, password: str) -> str:
        """Hash a password for storage; the password itself is never kept."""
        return self.hashes.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        """Say whether the password belongs to the stored hash."""
        return self.hashes.verify(password, password_hash)


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        """Read the current time."""
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TokenIdentifierFactory:
    """Ids nobody can guess."""

    def new_id(self) -> str:
        """Mint one url-safe token."""
        return secrets.token_urlsafe(_IDENTIFIER_BYTES)


@dataclass(frozen=True, slots=True)
class HmacSessionCookieSigner:
    """A cookie value is the session id and its HMAC-SHA256 over the instance key."""

    secret_key: bytes

    def sign(self, session_id: str) -> str:
        """Build the value the browser carries."""
        return f"{session_id}{_COOKIE_SEPARATOR}{self._signature(session_id)}"

    def session_id_from(self, cookie_value: str) -> str | None:
        """Return the id only for a value this instance signed."""
        session_id, separator, signature = cookie_value.rpartition(_COOKIE_SEPARATOR)
        if not separator or not hmac.compare_digest(
            signature,
            self._signature(session_id),
        ):
            return None
        return session_id

    def _signature(self, session_id: str) -> str:
        return hmac.new(self.secret_key, session_id.encode(), sha256).hexdigest()


def _user(account: Sequence[str]) -> User:
    user_id, username, role = account
    return User(id=user_id, username=username, role=Role(role))
