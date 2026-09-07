"""The capabilities the deck list calls out through; adapters fill them.

Named for decks rather than for a catalogue, because the message catalog
(ADR 0012) is a different thing entirely.
"""

from abc import abstractmethod
from datetime import datetime
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
    def folders(self, source: Source) -> tuple[DeckFolder, ...] | None:
        """Every folder at the source's newest commit, or nothing when it is unreadable.

        Nothing is not emptiness: a source nobody could read says nothing about
        what it carries, while a read that found no folder says every deck is
        gone.
        """


class DeckStore(Protocol):
    """The decks this instance knows about, keyed by their folder name."""

    @abstractmethod
    def put(self, deck: Deck) -> None:
        """Write the deck under its slug, replacing what that slug held."""

    @abstractmethod
    def all(self) -> tuple[Deck, ...]:
        """Every deck whose folder is still there, in no promised order."""

    @abstractmethod
    def mark_removed_except(self, present: frozenset[str], *, at: datetime) -> None:
        """Mark the decks outside `present` removed, and clear the mark inside it.

        A mark is not a delete: the row keeps its identity and its owner, so a
        folder pushed again is the deck it was.
        """
