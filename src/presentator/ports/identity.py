"""The capabilities the login calls out through; adapters fill them.

The stores, hasher, and cookie signer are this product's implementations of
the `webauth` ports (ADR 0003). First start and the first admin stay here
until `webauth[users]` ships them.
"""

from abc import abstractmethod
from datetime import datetime
from typing import Protocol

from presentator.contracts.models import Account, Session, User


class UserStore(Protocol):
    """Accounts an admin creates; first start is the only path without one."""

    @abstractmethod
    def get(self, user_id: str) -> User | None:
        """A live session names its user by id."""

    @abstractmethod
    def get_by_username(self, username: str) -> Account | None:
        """Login looks up the typed name, never an enumeration of accounts."""

    @abstractmethod
    def add_first_account(self, account: Account) -> None:
        """Store the instance's first account, refusing once one exists.

        Counting and inserting are one step, so two first starts at the same
        moment cannot both become admin.
        """

    @abstractmethod
    def create(self, username: str, password_hash: str, role: str) -> Account:
        """Add an account once an admin exists to create one."""

    @abstractmethod
    def count(self) -> int:
        """First start is offered only while no account exists."""

    @abstractmethod
    def first_admin(self) -> User | None:
        """Whoever set this instance up, and therefore owns what it configures."""


class SessionRecordStore(Protocol):
    """Server-side session rows; logout deletes the row, not only the cookie."""

    @abstractmethod
    def create(
        self,
        user_id: str,
        expires_at: datetime,
        *,
        ip_address: str,
        user_agent: str,
    ) -> Session:
        """Open a session; last_seen is this store's, from the clock it holds."""

    @abstractmethod
    def load(self, session_id: str) -> Session | None:
        """A cookie is only a key; the row is the session."""

    @abstractmethod
    def touch(
        self,
        record: Session,
        *,
        ip_address: str,
        user_agent: str,
        now: datetime,
    ) -> None:
        """Slide last_seen to `now` and remember where the request came from."""

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """Logout has to kill the row so a stolen cookie cannot come back."""

    @abstractmethod
    def delete_for_user(self, user_id: str) -> int:
        """End every session of one account; the number dropped is returned."""

    @abstractmethod
    def prune_overflow(self, user_id: str, max_sessions: int) -> list[str]:
        """Drop the oldest sessions above `max_sessions`, newest kept."""


class LoginAttemptStore(Protocol):
    """Failed attempts are counted so guessing is throttled."""

    @abstractmethod
    def record(self, *, ip_address: str, username: str, success: bool) -> None:
        """Remember one attempt; only a failure spends the budget."""

    @abstractmethod
    def count_recent_failures(
        self,
        *,
        ip_address: str,
        window_seconds: int,
        username: str | None = None,
    ) -> int:
        """Failures inside the window, by username when given, else by address."""


class PasswordHasher(Protocol):
    """Hashing is a port so Argon2id stays in the adapter."""

    @abstractmethod
    def hash(self, password: str) -> str:
        """First start and admin-created accounts store only the hash."""

    @abstractmethod
    def verify(self, password: str, stored_hash: str | None) -> bool:
        """Say whether the password belongs to the hash, and refuse an absent one.

        An absent hash still costs a full verification, so an unknown name and a
        wrong password cannot be told apart by how long the answer takes.
        """


class IdentifierFactory(Protocol):
    """Unguessable ids come from the adapter, so the application draws no randomness."""

    @abstractmethod
    def new_id(self) -> str:
        """A user id is minted here."""


class SessionCookieSigner(Protocol):
    """A cookie is a signed session id, so a tampered one dies before the store."""

    @abstractmethod
    def sign(self, session_id: str) -> str:
        """Turn a session id into the value the browser carries."""

    @abstractmethod
    def session_id_from(self, cookie_value: str) -> str | None:
        """Return the signed id, or nothing when the value was not signed here."""


class SessionLiveness(Protocol):
    """Whether a stored session still stands at a given instant."""

    @abstractmethod
    def admits(self, session: Session, *, at: datetime) -> bool:
        """Alive inside the idle window, including the boundary itself."""
