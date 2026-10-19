"""The capabilities the login calls out through; adapters fill them.

The stores are this product's implementations of the `webauth` ports (ADR
0003); the session cookie is the library's own (issue #105).
"""

from abc import abstractmethod
from datetime import datetime
from typing import Protocol

from presentator.contracts.models import Session, User


class UserStore(Protocol):
    """The people who may sign in."""

    @abstractmethod
    def get(self, user_id: str) -> User | None:
        """A live session names its user by id."""


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
    ) -> bool:
        """Slide last_seen to `now` and remember where the request came from.

        Renews only a row that still stands and says whether it did, so a
        session deleted after `load` is never written back.
        """

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """Logout has to kill the row so a stolen cookie cannot come back."""

    @abstractmethod
    def delete_for_user(self, user_id: str) -> int:
        """End every session of one account; the number dropped is returned."""

    @abstractmethod
    def prune_overflow(self, user_id: str, max_sessions: int) -> list[str]:
        """Drop the oldest sessions above `max_sessions`, newest kept."""


class IdentifierFactory(Protocol):
    """Unguessable ids come from the adapter, so the application draws no randomness."""

    @abstractmethod
    def new_id(self) -> str:
        """A deck or source id is minted here."""


class SessionLiveness(Protocol):
    """Whether a stored session still stands at a given instant."""

    @abstractmethod
    def admits(self, session: Session, *, at: datetime) -> bool:
        """Alive inside the idle window and the cap, boundaries included."""
