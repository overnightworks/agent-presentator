"""The capabilities the deck list calls out through; adapters fill them.

Named for decks rather than for a catalogue, because the message catalog
(ADR 0012) is a different thing entirely.
"""

from abc import abstractmethod
from datetime import datetime
from typing import Protocol

from presentator.contracts.decks import (
    Artefacts,
    Build,
    BuildAttempt,
    BuildFailure,
    ConnectionCheckResult,
    Deck,
    Source,
    SourcePoll,
    SourceRun,
    SourceWrite,
)


class SourceStore(Protocol):
    """The sources decks are mirrored from, one row each."""

    @abstractmethod
    def add(self, write: SourceWrite) -> Source | None:
        """Write a new source with its secrets, or nothing when name or URL is taken.

        The access secret is stored encrypted; the webhook secret is stored
        only as the hash. An existing row is not rewritten.
        """

    @abstractmethod
    def hook_secret_hash(self, name: str) -> bytes | None:
        """The stored hash of that source's webhook secret, if this name exists.

        The name is matched against stored names and is never a path.
        """

    @abstractmethod
    def put_credential(self, source_id: str, secret: str) -> None:
        """Replace that source's read-only secret with this value, encrypted.

        Nothing of the value is returned. A row that still named an
        environment variable keeps its identity and gains a stored secret.
        """

    @abstractmethod
    def put_hook_secret_hash(self, name: str, digest: bytes) -> bool:
        """Replace that source's webhook-secret hash, or nothing when it is missing."""

    @abstractmethod
    def all(self) -> tuple[Source, ...]:
        """Every source this instance mirrors, in no promised order."""


class DeckFolders(Protocol):
    """Reads the folders a source carries, without deciding what a deck is."""

    @abstractmethod
    def folders(self, source: Source) -> SourcePoll:
        """Poll the source for its newest folders, and the commit reached.

        No folder is not emptiness: a source nobody could read carries no
        folders and names why, while a read that found no folder carries an
        empty tuple and says every deck is gone.
        """


class ConnectionChecker(Protocol):
    """Probes an unsaved source's URL and secret, writing nothing to disk."""

    @abstractmethod
    def check(self, *, url: str, ref: str, secret: str) -> ConnectionCheckResult:
        """What answered: no failure and the head commit, or which failure and why."""


class SourceRuns(Protocol):
    """Every source's recent polls, bounded to the newest the page reads."""

    @abstractmethod
    def record(self, run: SourceRun) -> None:
        """Add this run and drop older ones of the same source past the bound."""

    @abstractmethod
    def newest(self, source_id: str) -> SourceRun | None:
        """That source's newest run, or nothing while it has never been polled."""

    @abstractmethod
    def recent(self, source_id: str) -> tuple[SourceRun, ...]:
        """That source's newest runs, newest first, no more than the page shows."""


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
        the new one, and never from a mixture of the two. The attempt that led
        to it is cleared by the same write: a talk that stands has nothing left
        to report about how it came about.
        """

    @abstractmethod
    def put_attempt(self, slug: str, attempt: BuildAttempt) -> None:
        """Record the build this deck last started, without touching its talk.

        What a deck delivers is the build's alone, so a run that says it has
        begun, and a run that says it failed, leave the standing talk and its
        PDF exactly where they are (line 16).
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
    def build(self, deck: Deck, *, source: Source) -> Artefacts | BuildFailure:
        """What this deck's commit built into, or why nothing came of it.

        A failure is what a broken deck yields, so the talk that already stands
        keeps standing until a build really produced a new one. The reason
        travels with the answer rather than staying in a log, because it is
        what the deck's page has to show its owner (line 9); a failure with no
        words is one no toolchain reported back about.
        """

    @abstractmethod
    def holds(self, artefacts: Artefacts) -> bool:
        """Whether both artefacts really stand under the root builds are kept in.

        A deck is code (ADR 0005), so where a build says it wrote is checked
        rather than trusted before that place becomes an address.
        """


class ToolchainThemes(Protocol):
    """The theme names this instance's toolchain project carries (ADR 0014)."""

    @abstractmethod
    def names(self) -> tuple[str, ...] | None:
        """Every theme name the toolchain builds with, or nothing while unreadable.

        A deck page reads nothing as no row to show, never as an empty set
        (R3).
        """
