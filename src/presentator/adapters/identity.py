"""SQLite rows and Argon2id hashes behind the identity ports.

The stores and hasher are this product's implementations of the `webauth`
ports (ADR 0003). First start's write lock stays here until `webauth[users]`
ships it.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from webauth.liveness import IdleWindowLiveness

from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.models import (
    Account,
    FirstStartClosedError,
    Role,
    Session,
    User,
)
from presentator.ports.clock import Clock

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
    last_seen TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS login_attempts (
    username TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    failed_at TEXT NOT NULL
);
"""
_IDENTIFIER_BYTES: Final = 32
_FIRST_START_IS_OVER: Final = "this instance already has an account"
_SESSION_COLUMNS: Final = (
    ("ip_address", "TEXT NOT NULL DEFAULT ''"),
    ("user_agent", "TEXT NOT NULL DEFAULT ''"),
)
_ATTEMPT_COLUMNS: Final = (("ip_address", "TEXT NOT NULL DEFAULT ''"),)


def _argon2id() -> PasswordHash:
    return PasswordHash((Argon2Hasher(),))


def _hash_of_a_secret_nobody_typed() -> str:
    return _argon2id().hash(secrets.token_urlsafe(_IDENTIFIER_BYTES))


def create_identity_tables(database: Path) -> None:
    """Make the accounts, sessions, and attempts tables exist."""
    apply_schema(database, _SCHEMA)
    _ensure_columns(database, "sessions", _SESSION_COLUMNS)
    _ensure_columns(database, "login_attempts", _ATTEMPT_COLUMNS)


def _ensure_columns(
    database: Path,
    table: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    with rows(database) as cursor:
        named = {
            str(column[1])
            for column in cursor.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns:
            if name not in named:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


@dataclass(frozen=True, slots=True)
class SqliteUserStore:
    """The accounts table."""

    database: Path

    def get(self, user_id: str) -> User | None:
        """Read the account a live session belongs to."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, username, role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return None if row is None else _user(row)

    def get_by_username(self, username: str) -> Account | None:
        """Read the account and its hash for the name someone typed."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, username, role, password_hash FROM users"
                " WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        user_id, stored_name, role, password_hash = row
        return Account(
            id=user_id,
            username=stored_name,
            role=Role(role),
            password_hash=password_hash,
        )

    def add_first_account(self, account: Account) -> None:
        """Count and insert inside one write lock, so first start happens once."""
        with rows(self.database) as cursor:
            cursor.execute("BEGIN IMMEDIATE")
            (accounts,) = cursor.execute("SELECT count(*) FROM users").fetchone()
            if accounts:
                raise FirstStartClosedError(_FIRST_START_IS_OVER)
            cursor.execute(
                "INSERT INTO users (id, username, role, password_hash)"
                " VALUES (?, ?, ?, ?)",
                (
                    account.id,
                    account.username,
                    account.role.value,
                    account.password_hash,
                ),
            )

    def create(self, username: str, password_hash: str, role: str) -> Account:
        """Add an account once the instance already has one."""
        user_id = secrets.token_urlsafe(_IDENTIFIER_BYTES)
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO users (id, username, role, password_hash)"
                " VALUES (?, ?, ?, ?)",
                (user_id, username, role, password_hash),
            )
        return Account(
            id=user_id,
            username=username,
            role=Role(role),
            password_hash=password_hash,
        )

    def count(self) -> int:
        """How many accounts exist, which is what first start asks."""
        with rows(self.database) as cursor:
            (accounts,) = cursor.execute("SELECT count(*) FROM users").fetchone()
        return int(accounts)

    def first_admin(self) -> User | None:
        """The account first start created, or nothing while none exists."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, username, role FROM users WHERE role = ?"
                " ORDER BY rowid LIMIT 1",
                (Role.ADMIN.value,),
            ).fetchone()
        return None if row is None else _user(row)


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        """Read the current time."""
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SqliteSessionRecordStore:
    """The sessions table."""

    database: Path
    clock: Clock = field(default_factory=SystemClock)

    def create(
        self,
        user_id: str,
        expires_at: datetime,
        *,
        ip_address: str,
        user_agent: str,
    ) -> Session:
        """Open a session whose last_seen is now; idle-window has no expiry column."""
        del expires_at
        session = Session(
            id=secrets.token_urlsafe(_IDENTIFIER_BYTES),
            user_id=user_id,
            last_seen=self.clock.now(),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._put(session)
        return session

    def load(self, session_id: str) -> Session | None:
        """Read the row a cookie points at."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, user_id, last_seen, ip_address, user_agent"
                " FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        stored_id, user_id, last_seen, ip_address, user_agent = row
        return Session(
            id=stored_id,
            user_id=user_id,
            last_seen=datetime.fromisoformat(last_seen),
            ip_address=ip_address,
            user_agent=user_agent,
        )

    def touch(
        self,
        record: Session,
        *,
        ip_address: str,
        user_agent: str,
        now: datetime,
    ) -> None:
        """Write last_seen and origin in place."""
        self._put(
            Session(
                id=record.id,
                user_id=record.user_id,
                last_seen=now,
                ip_address=ip_address,
                user_agent=user_agent,
            ),
        )

    def delete(self, session_id: str) -> None:
        """Delete the row, so logging out cannot be undone with the old cookie."""
        with rows(self.database) as cursor:
            cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_for_user(self, user_id: str) -> int:
        """End every session of one account."""
        with rows(self.database) as cursor:
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return int(cursor.rowcount)

    def prune_overflow(self, user_id: str, max_sessions: int) -> list[str]:
        """Drop the oldest sessions above the cap, newest kept."""
        with rows(self.database) as cursor:
            stored = cursor.execute(
                "SELECT id FROM sessions WHERE user_id = ?"
                " ORDER BY last_seen DESC, id DESC",
                (user_id,),
            ).fetchall()
            dropped = [session_id for (session_id,) in stored[max_sessions:]]
            for session_id in dropped:
                cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return dropped

    def _put(self, session: Session) -> None:
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO sessions"
                " (id, user_id, last_seen, ip_address, user_agent)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " last_seen = excluded.last_seen,"
                " ip_address = excluded.ip_address,"
                " user_agent = excluded.user_agent",
                (
                    session.id,
                    session.user_id,
                    session.last_seen.isoformat(),
                    session.ip_address,
                    session.user_agent,
                ),
            )


@dataclass(frozen=True, slots=True)
class SqliteLoginAttemptStore:
    """The failed-attempts table."""

    database: Path
    clock: Clock = field(default_factory=SystemClock)

    def record(self, *, ip_address: str, username: str, success: bool) -> None:
        """Remember a refused attempt; a success spends nothing."""
        if success:
            return
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO login_attempts (username, ip_address, failed_at)"
                " VALUES (?, ?, ?)",
                (username, ip_address, self.clock.now().isoformat()),
            )

    def count_recent_failures(
        self,
        *,
        ip_address: str,
        window_seconds: int,
        username: str | None = None,
    ) -> int:
        """Count refused attempts inside the caller's window."""
        since = (self.clock.now() - timedelta(seconds=window_seconds)).isoformat()
        with rows(self.database) as cursor:
            if username is None:
                (failures,) = cursor.execute(
                    "SELECT count(*) FROM login_attempts"
                    " WHERE ip_address = ? AND failed_at >= ?",
                    (ip_address, since),
                ).fetchone()
            else:
                (failures,) = cursor.execute(
                    "SELECT count(*) FROM login_attempts"
                    " WHERE username = ? AND failed_at >= ?",
                    (username, since),
                ).fetchone()
        return int(failures)


@dataclass(frozen=True, slots=True)
class Argon2PasswordHasher:
    """Argon2id, named rather than taken from a recommendation that may move."""

    hashes: PasswordHash = field(default_factory=_argon2id)
    hash_for_nobody: str = field(default_factory=_hash_of_a_secret_nobody_typed)

    def hash(self, password: str) -> str:
        """Hash a password for storage; the password itself is never kept."""
        return self.hashes.hash(password)

    def verify(self, password: str, stored_hash: str | None) -> bool:
        """Say whether the password belongs to the stored hash."""
        if stored_hash is None:
            # The answer is known; the point is that a name nobody has costs the
            # same Argon2id verification as a name somebody has.
            self.hashes.verify(password, self.hash_for_nobody)
            return False
        return self.hashes.verify(password, stored_hash)


@dataclass(frozen=True, slots=True)
class TokenIdentifierFactory:
    """Ids nobody can guess."""

    def new_id(self) -> str:
        """Mint one url-safe token."""
        return secrets.token_urlsafe(_IDENTIFIER_BYTES)


@dataclass
class _LastSeen:
    """A writable last_seen, which is the field the idle-window policy reads."""

    last_seen: datetime


@dataclass(frozen=True, slots=True)
class IdleWindow:
    """The library's last_seen window, held so the application never names it."""

    policy: IdleWindowLiveness

    def admits(self, session: Session, *, at: datetime) -> bool:
        """Alive inside the window, including the boundary itself."""
        return self.policy.admits_stored_session(_LastSeen(session.last_seen), at)


def _user(account: Sequence[str]) -> User:
    user_id, username, role = account
    return User(id=user_id, username=username, role=Role(role))
