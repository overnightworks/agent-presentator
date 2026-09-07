"""Taking decks in from a source, building them, and what the lobby then shows.

Which folder counts as a deck, which deck is out of date, when a build may
become the talk that is delivered, and what a deck page says is decided here;
git, TOML, SQL, and the build toolchain stay outside (ADR 0001, ADR 0005).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Final

from presentator.contracts.decks import (
    SLIDES_FILE,
    Build,
    Deck,
    DeckPage,
    ListedDeck,
    Source,
)
from presentator.ports.clock import Clock
from presentator.ports.decks import BuildRunner, DeckFolders, DeckStore, SourceStore

_log = logging.getLogger(__name__)

# A person compares the commit on the page with the one their push wrote, and
# reads it off the screen; the first characters are what git itself shows.
_SHORT_COMMIT: Final = 7
# A folder's name is one path element: it stays one word of a header value and
# never reaches past the directory it is joined under.
_NAMES_NO_FOLDER: Final = frozenset({"", ".", ".."})
_NEVER_IN_A_FOLDER_NAME: Final = frozenset('/\\"')
_NOT_A_FOLDERS_OWN_NAME: Final = "folder %r is not a folder's own name, skipped"
_NAME_BELONGS_TO_ANOTHER_SOURCE: Final = (
    "folder %r is already carried by another source, skipped for source %s"
)


def _is_a_plain_folder_name(candidate: str) -> bool:
    """Whether the candidate is a folder's own name and nothing besides."""
    return (
        candidate not in _NAMES_NO_FOLDER
        and candidate.isprintable()
        and not _NEVER_IN_A_FOLDER_NAME.intersection(candidate)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Decks:
    """Every use case the lobby has around the talks it knows."""

    sources: SourceStore
    folders: DeckFolders
    store: DeckStore
    builder: BuildRunner
    clock: Clock
    _one_at_a_time: Lock = field(default_factory=Lock)

    def refresh(self) -> None:
        """Take the source in and build what changed, one refresh at a time.

        Whoever noticed the push calls this. A further request while one runs is
        already served by it, so a flood of them pulls once rather than once
        each, and the builds of one refresh follow one another instead of
        competing for the machine.
        """
        if not self._one_at_a_time.acquire(blocking=False):
            return
        try:
            self._take_in_and_build()
        finally:
            self._one_at_a_time.release()

    def listed(self) -> tuple[ListedDeck, ...]:
        """The stored decks, newest changed first, reading no source.

        A page view costs a read, never a pull: what the source carries is the
        refresh's business.
        """
        now = self.clock.now()
        newest_first = sorted(
            self.store.all(),
            key=lambda deck: deck.changed_at,
            reverse=True,
        )
        return tuple(
            ListedDeck(slug=deck.slug, title=deck.title, age=now - deck.changed_at)
            for deck in newest_first
        )

    def page(self, slug: str) -> DeckPage | None:
        """What that deck's page says, or nothing while no deck carries the slug.

        The commit named is the one the delivered talk was built from, not the
        one the source carries now: a person reads it to see whether their push
        is in what is on the screen (line 7), and while nothing is built there
        is nothing on the screen but what the source last carried.
        """
        deck = self.store.get(slug)
        if deck is None:
            return None
        built = deck.build
        return DeckPage(
            slug=deck.slug,
            title=deck.title,
            source=self._address_of(deck),
            commit=(deck.commit if built is None else built.commit)[:_SHORT_COMMIT],
            built_ago=None if built is None else self.clock.now() - built.built_at,
        )

    def _address_of(self, deck: Deck) -> str | None:
        """The address the source that carried this deck is mirrored from.

        A deck names the source it came from, never whichever source the
        instance happens to have first, so two sources cannot show one
        another's address on a page.
        """
        return next(
            (
                source.url
                for source in self.sources.all()
                if source.id == deck.source_id
            ),
            None,
        )

    def built_talk(self, slug: str) -> Path | None:
        """The directory the deck's talk is delivered from, while one is built."""
        deck = self.store.get(slug)
        if deck is None or deck.build is None:
            return None
        return deck.build.directory

    def exported_pdf(self, slug: str) -> Path | None:
        """The file the deck's PDF is handed over as, while one is exported.

        A slug that is not a folder's plain name names no deck here: the export
        is offered under that name, so a separator, a quote, or a line break in
        it would otherwise reach the answer's own header.
        """
        if not _is_a_plain_folder_name(slug):
            return None
        deck = self.store.get(slug)
        if deck is None or deck.build is None:
            return None
        return deck.build.pdf

    def source_address(self) -> str | None:
        """The git address an empty list names, while one source is all there is.

        With a second source there is no single address the list could name,
        so it says only that it is empty.
        """
        sources = self.sources.all()
        return sources[0].url if len(sources) == 1 else None

    def _take_in_and_build(self) -> None:
        """Make the configured source a row, then walk the sources one by one.

        A source is taken in and built before the next one is read, so a
        refresh costs its sources' pull bounds one after another instead of
        opening as many pulls at once as the instance has sources.
        """
        self.sources.seed()
        for source in self.sources.all():
            self._take_in(source)
            self._build_what_changed(source)

    def _take_in(self, source: Source) -> None:
        """Store every folder that carries both a manifest and slides.

        The folder name becomes the slug, so a changed title reaches no address.
        A folder whose name is not a folder's own name — such as `..`, planted
        as a git tree entry — is skipped here rather than stored, so it is never
        used as a path component by this deck or its build. A folder the source
        has stopped carrying is marked as removed rather than deleted, and a
        folder that comes back under its old name loses that mark, so it is the
        deck it was. A source nobody could read carries no such news, and leaves
        every deck where it is. A folder whose name another source already
        carries is skipped too: the slug is the address of one deck on this
        instance, and the source that carried it first keeps it.
        """
        carried = self.folders.folders(source)
        if carried is None:
            return
        for folder in carried:
            if not _is_a_plain_folder_name(folder.name):
                _log.warning(_NOT_A_FOLDERS_OWN_NAME, folder.name)
                continue
            if folder.title is None or SLIDES_FILE not in folder.file_names:
                continue
            taken_in = self.store.put(
                Deck(
                    slug=folder.name,
                    title=folder.title,
                    changed_at=folder.changed_at,
                    owner_id=source.owner_id,
                    source_id=source.id,
                    commit=folder.commit,
                    build=None,
                ),
            )
            if not taken_in:
                _log.warning(_NAME_BELONGS_TO_ANOTHER_SOURCE, folder.name, source.name)
        self.store.mark_removed_except(
            present=frozenset(folder.name for folder in carried),
            source_id=source.id,
            at=self.clock.now(),
        )

    def _build_what_changed(self, source: Source) -> None:
        """Build that source's decks whose commit its talk was not built from.

        The commit is the whole change check: a push moves one folder's commit,
        so only that deck is built again and the other talks are left alone. A
        deck is built out of the mirror of its own source, so a source's walk
        passes over the decks another source carried.
        """
        for deck in self.store.all():
            if deck.source_id != source.id:
                continue
            if deck.build is not None and deck.build.commit == deck.commit:
                continue
            self._switch_over(deck, source)

    def _switch_over(self, deck: Deck, source: Source) -> None:
        """Deliver this deck from its new build, once there really is one.

        A build that failed, and one that left artefacts anywhere but under the
        root they belong in, write no pointer at all: whatever the deck was
        delivering before keeps being delivered.
        """
        artefacts = self.builder.build(deck, source=source)
        if artefacts is None or not self.builder.holds(artefacts):
            return
        self.store.put_build(
            deck.slug,
            Build(
                directory=artefacts.directory,
                pdf=artefacts.pdf,
                commit=deck.commit,
                built_at=self.clock.now(),
            ),
        )
