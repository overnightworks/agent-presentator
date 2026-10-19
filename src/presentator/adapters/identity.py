"""SQLite rows behind the identity ports.

The stores are this product's implementations of the `webauth` ports
(ADR 0003).
"""

import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from webauth.liveness import IdleWindowLiveness

from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.models import Role, Session, User
from presentator.ports.clock import Clock

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);
"""
_IDENTIFIER_BYTES: Final = 32


class OldDatabaseError(RuntimeError):
    """The file was written before the current identity schema."""


_OLD_VERSION_REFUSAL: Final = (
    "%s was written by an older version (login_attempts, or a users or"
    " sessions table with other columns than this version's). Take the backup,"
    " then move the database and its -wal and -shm files aside so the instance"
    " starts a fresh one (docs/OPERATIONS.md, 'Starting over from an old"
    " database')."
)


def create_identity_tables(database: Path) -> None:
    """Make the accounts and sessions tables exist in a file no older version wrote."""
    _refuse_an_old_database(database)
    apply_schema(database, _SCHEMA)


def _refuse_an_old_database(database: Path) -> None:
    """Look without writing: the main file and its -wal never change.

    A -wal without frames is looked at immutable, so no side file appears. A
    -wal with frames is looked at read-only only, because an immutable look
    would miss a schema committed solely there; SQLite may then create -shm.
    """
    if not database.exists():
        return
    write_ahead_log = database.with_name(database.name + "-wal")
    has_logged_frames = write_ahead_log.exists() and write_ahead_log.stat().st_size > 0
    options = "mode=ro" if has_logged_frames else "mode=ro&immutable=1"
    connection = sqlite3.connect(f"{database.absolute().as_uri()}?{options}", uri=True)
    try:
        cursor = connection.cursor()
        tables = _table_names(cursor)
        is_old = "login_attempts" in tables or any(
            table in tables and _column_names(cursor, table) != columns
            for table, columns in _fresh_columns().items()
        )
    finally:
        connection.close()
    if is_old:
        raise OldDatabaseError(_OLD_VERSION_REFUSAL % database)


def _fresh_columns() -> dict[str, set[str]]:
    fresh = sqlite3.connect(":memory:")
    try:
        fresh.executescript(_SCHEMA)
        cursor = fresh.cursor()
        return {table: _column_names(cursor, table) for table in _table_names(cursor)}
    finally:
        fresh.close()


def _table_names(cursor: sqlite3.Cursor) -> set[str]:
    return {
        str(table[0])
        for table in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        ).fetchall()
    }


def _column_names(cursor: sqlite3.Cursor, table: str) -> set[str]:
    return {
        str(column[1])
        for column in cursor.execute(f"PRAGMA table_info({table})").fetchall()
    }


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
        """Open a session created and last seen now; the policy reads no expiry."""
        del expires_at
        opened_at = self.clock.now()
        session = Session(
            id=secrets.token_urlsafe(_IDENTIFIER_BYTES),
            user_id=user_id,
            created_at=opened_at,
            last_seen=opened_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO sessions"
                " (id, user_id, created_at, last_seen, ip_address, user_agent)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.user_id,
                    session.created_at.isoformat(),
                    session.last_seen.isoformat(),
                    session.ip_address,
                    session.user_agent,
                ),
            )
        return session

    def load(self, session_id: str) -> Session | None:
        """Read the row a cookie points at."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT id, user_id, created_at, last_seen, ip_address, user_agent"
                " FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        stored_id, user_id, created_at, last_seen, ip_address, user_agent = row
        return Session(
            id=stored_id,
            user_id=user_id,
            created_at=datetime.fromisoformat(created_at),
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
    ) -> bool:
        """Renew the row only while it still stands; a deleted session stays gone."""
        with rows(self.database) as cursor:
            cursor.execute(
                "UPDATE sessions SET last_seen = ?, ip_address = ?, user_agent = ?"
                " WHERE id = ?",
                (now.isoformat(), ip_address, user_agent, record.id),
            )
            return cursor.rowcount == 1

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


@dataclass(frozen=True, slots=True)
class TokenIdentifierFactory:
    """Ids nobody can guess."""

    def new_id(self) -> str:
        """Mint one url-safe token."""
        return secrets.token_urlsafe(_IDENTIFIER_BYTES)


@dataclass
class _StoredTimes:
    """The two fields the idle-window policy reads off a stored record."""

    created_at: datetime
    last_seen: datetime


def lifetime_policy(lifetime: timedelta) -> IdleWindowLiveness:
    """A sign-in lasts `lifetime` from the moment it opened, used or not."""
    seconds = int(lifetime.total_seconds())
    return IdleWindowLiveness(seconds, absolute_max_age_seconds=seconds)


@dataclass(frozen=True, slots=True)
class IdleWindow:
    """The library's idle window and cap, held so the application never names it."""

    policy: IdleWindowLiveness

    def admits(self, session: Session, *, at: datetime) -> bool:
        """Alive inside the window and the cap, boundaries included."""
        return self.policy.admits_stored_session(
            _StoredTimes(session.created_at, session.last_seen), at
        )


def _user(row: Sequence[str]) -> User:
    user_id, username, role = row
    return User(id=user_id, username=username, role=Role(role))
