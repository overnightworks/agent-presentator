"""The capabilities the login calls out through; adapters fill them.

The whole set is a bridge to `webauth` (ADR 0003): songmaker #835 brings the
stores and #833 the user management, and this file goes with them.
"""

from abc import abstractmethod
from datetime import datetime
from typing import Protocol

from presentator.contracts.models import Credentials, Session, User


class UserStore(Protocol):
    """Accounts an admin creates; first start is the only path without one."""

    @abstractmethod
    def get(self, user_id: str) -> User | None:
        """A live session names its user by id."""

    @abstractmethod
    def credentials_for(self, username: str) -> Credentials | None:
        """Login looks up the typed name, never an enumeration of accounts."""

    @abstractmethod
    def add_first_account(self, credentials: Credentials) -> None:
        """Store the instance's first account, refusing once one exists.

        Counting and inserting are one step, so two first starts at the same
        moment cannot both become admin.
        """

    @abstractmethod
    def count(self) -> int:
        """First start is offered only while no account exists."""


class SessionRecordStore(Protocol):
    """Server-side session rows; logout deletes the row, not only the cookie."""

    @abstractmethod
    def get(self, session_id: str) -> Session | None:
        """A cookie is only a key; the row is the session."""

    @abstractmethod
    def put(self, session: Session) -> None:
        """Create and sliding touch are the same write of an immutable record."""

    @abstractmethod
    def remove(self, session_id: str) -> None:
        """Logout has to kill the row so a stolen cookie cannot come back."""


class LoginAttemptStore(Protocol):
    """Failed attempts are counted so guessing is throttled."""

    @abstractmethod
    def record_failure(self, username: str, *, at: datetime) -> None:
        """The clock is passed in so the store does not read one of its own."""

    @abstractmethod
    def failure_count(self, username: str, *, since: datetime) -> int:
        """The window is the caller's, so policy does not leak into the store."""


class PasswordHasher(Protocol):
    """Hashing is a port so Argon2id stays in the adapter."""

    @abstractmethod
    def hash(self, password: str) -> str:
        """First start and admin-created accounts store only the hash."""

    @abstractmethod
    def verify(self, password: str, password_hash: str | None) -> bool:
        """Say whether the password belongs to the hash, and refuse an absent one.

        An absent hash still costs a full verification, so an unknown name and a
        wrong password cannot be told apart by how long the answer takes.
        """


class Clock(Protocol):
    """Time enters here so a test can freeze it."""

    @abstractmethod
    def now(self) -> datetime:
        """Callers must not read the system clock themselves."""


class IdentifierFactory(Protocol):
    """Unguessable ids come from the adapter, so the application draws no randomness."""

    @abstractmethod
    def new_id(self) -> str:
        """A user id and a session id are both minted here."""


class SessionCookieSigner(Protocol):
    """A cookie is a signed session id, so a tampered one dies before the store."""

    @abstractmethod
    def sign(self, session_id: str) -> str:
        """Turn a session id into the value the browser carries."""

    @abstractmethod
    def session_id_from(self, cookie_value: str) -> str | None:
        """Return the signed id, or nothing when the value was not signed here."""
