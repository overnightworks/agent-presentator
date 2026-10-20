"""The capability the login calls out through; an adapter fills it."""

from abc import abstractmethod
from typing import Protocol


class IdentifierFactory(Protocol):
    """Unguessable ids come from the adapter, so the application draws no randomness."""

    @abstractmethod
    def new_id(self) -> str:
        """A deck or source id is minted here."""
