"""The capabilities the deck list calls out through; adapters fill them.

Named for decks rather than for a catalogue, because the message catalog
(ADR 0012) is a different thing entirely.
"""

from abc import abstractmethod
from typing import Protocol

from presentator.contracts.decks import Deck, DeckFolder, Source


class SourceStore(Protocol):
    """Where decks are mirrored from; configuration until Settings owns it."""

    @abstractmethod
    def configured(self) -> Source | None:
        """The source this installation reads, or nothing while none is set."""


class DeckFolders(Protocol):
    """Reads the folders a source carries, without deciding what a deck is."""

    @abstractmethod
    def folders(self, source: Source) -> tuple[DeckFolder, ...]:
        """Every folder at the source's newest commit, empty when it is unreadable."""


class DeckStore(Protocol):
    """The decks this instance knows about, keyed by their folder name."""

    @abstractmethod
    def put(self, deck: Deck) -> None:
        """Write the deck under its slug, replacing what that slug held."""

    @abstractmethod
    def all(self) -> tuple[Deck, ...]:
        """Every stored deck, in no promised order."""
