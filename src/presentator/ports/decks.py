"""The capabilities the deck list calls out through; adapters fill them.

Named for decks rather than for a catalogue, because the message catalog
(ADR 0012) is a different thing entirely.
"""

from abc import abstractmethod
from datetime import datetime
from typing import Protocol

from presentator.contracts.decks import Artefacts, Build, Deck, DeckFolder, Source


class SourceStore(Protocol):
    """The sources decks are mirrored from, one row each."""

    @abstractmethod
    def seed(self) -> None:
        """Make the source the configuration names a row of its own, once.

        There is no surface to add a source at yet, so the row an installation
        already runs on is written from what it is configured with; writing it
        again writes nothing.
        """

    @abstractmethod
    def all(self) -> tuple[Source, ...]:
        """Every source this instance mirrors, in no promised order."""


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
    def put(self, deck: Deck) -> bool:
        """Write what a source carries under this slug, keeping its built talk.

        Taking a deck in must not unpresent it and must not take away its
        downloadable PDF, so what a build wrote moves only by putting a build.

        A folder name is one address for the whole instance, so a name another
        source already carries is refused rather than taken over: the answer
        says whether the slug was this deck's source's to write.
        """

    @abstractmethod
    def put_build(self, slug: str, build: Build) -> None:
        """Make that build the talk this deck delivers, in one write.

        Everything the build is switches at once, so nothing can read half a
        switch: an address that answered from the previous build answers from
        the new one, and never from a mixture of the two.
        """

    @abstractmethod
    def get(self, slug: str) -> Deck | None:
        """The deck under that slug, or nothing while no folder carries it."""

    @abstractmethod
    def all(self) -> tuple[Deck, ...]:
        """Every deck whose folder is still there, in no promised order."""

    @abstractmethod
    def mark_removed_except(
        self,
        present: frozenset[str],
        *,
        source_id: str,
        at: datetime,
    ) -> None:
        """Mark that source's decks outside `present` removed, clear it inside.

        A mark is not a delete: the row keeps its identity and its owner, so a
        folder pushed again is the deck it was. One source's folders say
        nothing about another's, so only the named source's decks are read.
        """


class BuildRunner(Protocol):
    """Builds one deck's folder into the talk that deck delivers.

    Where the folder comes from and which toolchain turns it into a talk is the
    adapter's business; the use case only says which deck is to be built.
    """

    @abstractmethod
    def build(self, deck: Deck, *, source: Source) -> Artefacts | None:
        """What this deck's commit built into, or nothing when the build failed.

        Nothing is what a broken deck yields, so the talk that already stands
        keeps standing until a build really produced a new one.
        """

    @abstractmethod
    def holds(self, artefacts: Artefacts) -> bool:
        """Whether both artefacts really stand under the root builds are kept in.

        A deck is code that runs on this host until it is sandboxed
        (ADR 0005), so where a build says it wrote is checked rather than
        trusted before that place becomes an address.
        """
