"""SQLite rows behind the identity ports.

The stores are this product's implementations of the `webauth` ports
(ADR 0003).
"""

import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.models import Role, Session, User

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
    expires_at TEXT NOT NULL
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
class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        """Read the current time."""
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SqliteSessionRecordStore:
    """The sessions table, joined to the accounts.

    Instants are stored as UTC ISO 8601 text, so comparing the text orders the
    instants. Every call commits on its own.
    """

    database: Path

    def create(self, user_id: str, *, now: datetime, expires_at: datetime) -> Session:
        """Open a session created at `now` that stands until `expires_at`."""
        session_id = secrets.token_urlsafe(_IDENTIFIER_BYTES)
        with rows(self.database) as cursor:
            account = cursor.execute(
                "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if account is None:
                message = f"no account {user_id} to open a session for"
                raise LookupError(message)
            cursor.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, user_id, _stored(now), _stored(expires_at)),
            )
        return Session(
            id=session_id,
            user=_user(account),
            created_at=now,
            expires_at=expires_at,
        )

    def load(self, session_id: str) -> Session | None:
        """Read the row a cookie points at with its account; no account, no session."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT sessions.id, sessions.created_at, sessions.expires_at,"
                " users.id, users.username, users.role"
                " FROM sessions JOIN users ON users.id = sessions.user_id"
                " WHERE sessions.id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        stored_id, created_at, expires_at, *account = row
        return Session(
            id=stored_id,
            user=_user(account),
            created_at=datetime.fromisoformat(created_at),
            expires_at=datetime.fromisoformat(expires_at),
        )

    def touch(self, session_id: str, *, expires_at: datetime) -> bool:
        """Renew the row only while it still stands; a deleted session stays gone."""
        with rows(self.database) as cursor:
            cursor.execute(
                "UPDATE sessions SET expires_at = ? WHERE id = ?",
                (_stored(expires_at), session_id),
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

    def prune_overflow(self, user_id: str, keep_newest: int) -> None:
        """End all but the `keep_newest` newest sessions by creation."""
        with rows(self.database) as cursor:
            cursor.execute(
                "DELETE FROM sessions WHERE user_id = ? AND id NOT IN ("
                "SELECT id FROM sessions WHERE user_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT ?)",
                (user_id, user_id, keep_newest),
            )


def _stored(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class TokenIdentifierFactory:
    """Ids nobody can guess."""

    def new_id(self) -> str:
        """Mint one url-safe token."""
        return secrets.token_urlsafe(_IDENTIFIER_BYTES)


def _user(row: Sequence[str]) -> User:
    user_id, username, role = row
    return User(id=user_id, username=username, role=Role(role))
