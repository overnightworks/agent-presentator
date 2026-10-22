"""The id minting deck and source creation call out through; an adapter fills it."""

from abc import abstractmethod
from typing import Protocol

from presentator.contracts.models import User
from presentator.contracts.people import SessionTimes


class IdentifierFactory(Protocol):
    """Unguessable ids come from the adapter, so the application draws no randomness."""

    @abstractmethod
    def new_id(self) -> str:
        """A deck or source id is minted here."""


class PersonAccounts(Protocol):
    """The account store, as far as erasing a person needs it."""

    @abstractmethod
    def get(self, user_id: str) -> User | None:
        """The account of that id; asking never creates one."""

    @abstractmethod
    def delete_account(self, user_id: str) -> None:
        """Delete the account and its remaining sessions together."""


class PersonSessions(Protocol):
    """The sign-in records of one person, without anything that could resume one."""

    @abstractmethod
    def sessions_of(self, user_id: str) -> tuple[SessionTimes, ...]:
        """When each sign-in of that account began and ends."""

    @abstractmethod
    def refresh_tokens_held(self, user_id: str) -> int:
        """How many of that account's sessions hold a refresh token, not the tokens."""

    @abstractmethod
    def delete_for_user(self, user_id: str) -> int:
        """End every session of that account; how many went."""
