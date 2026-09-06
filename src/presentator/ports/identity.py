"""Login talks to these capabilities; the adapter that fills them is a later slice."""

from datetime import datetime
from typing import Protocol

from presentator.contracts.models import Session, User


class UserStore(Protocol):
    """Accounts an admin creates; first-start is the only path without one."""

    def get(self, user_id: str) -> User | None:
        """A live session names a user by id."""
        ...  # pragma: no cover  # Protocol body is never executed

    def get_by_username(self, username: str) -> User | None:
        """Login looks up the typed name, never an enumeration of accounts."""
        ...  # pragma: no cover  # Protocol body is never executed

    def put(self, user: User, *, password_hash: str) -> None:
        """The hash stays beside the user, never on the User record."""
        ...  # pragma: no cover  # Protocol body is never executed

    def count(self) -> int:
        """First-start is offered only while no account exists."""
        ...  # pragma: no cover  # Protocol body is never executed


class SessionRecordStore(Protocol):
    """Server-side session rows; logout deletes the row, not only the cookie."""

    def get(self, session_id: str) -> Session | None:
        """A cookie is only a key; the row is the session."""
        ...  # pragma: no cover  # Protocol body is never executed

    def put(self, session: Session) -> None:
        """Create and sliding-touch are the same write of an immutable record."""
        ...  # pragma: no cover  # Protocol body is never executed

    def remove(self, session_id: str) -> None:
        """Logout must kill the row so a stolen cookie cannot come back."""
        ...  # pragma: no cover  # Protocol body is never executed


class LoginAttemptStore(Protocol):
    """Failed attempts are counted so guessing is throttled."""

    def record_failure(self, username: str, *, at: datetime) -> None:
        """The clock is passed in so the store does not read one of its own."""
        ...  # pragma: no cover  # Protocol body is never executed

    def failure_count(self, username: str, *, since: datetime) -> int:
        """The window is the caller's, so policy does not leak into the store."""
        ...  # pragma: no cover  # Protocol body is never executed


class PasswordHasher(Protocol):
    """Hashing is a port so Argon2id stays in the adapter."""

    def hash(self, password: str) -> str:
        """First-start and admin-created accounts store only the hash."""
        ...  # pragma: no cover  # Protocol body is never executed

    def verify(self, password: str, password_hash: str) -> bool:
        """Login compares against the hash, never the original password."""
        ...  # pragma: no cover  # Protocol body is never executed


class Clock(Protocol):
    """Time enters here so tests can freeze it."""

    def now(self) -> datetime:
        """Callers must not read the system clock themselves."""
        ...  # pragma: no cover  # Protocol body is never executed
