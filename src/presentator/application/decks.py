"""Taking decks in from a source, the list a person reads, and one deck's page.

Which folder counts as a deck, in which order the decks are shown, and what a
deck page says is decided here; git, TOML, and SQL stay outside (ADR 0001,
ADR 0005).
"""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Final

from presentator.contracts.decks import SLIDES_FILE, Deck, DeckPage, ListedDeck
from presentator.ports.clock import Clock
from presentator.ports.decks import DeckFolders, DeckStore, SourceStore

# A person compares the commit on the page with the one their push wrote, and
# reads it off the screen; the first characters are what git itself shows.
_SHORT_COMMIT: Final = 7


@dataclass(frozen=True, slots=True, kw_only=True)
class Decks:
    """Every use case the lobby has around the talks it knows."""

    sources: SourceStore
    folders: DeckFolders
    store: DeckStore
    clock: Clock
    _one_at_a_time: Lock = field(default_factory=Lock)

    def refresh(self) -> None:
        """Take in what the source carries now, one refresh at a time.

        Whoever noticed the push calls this. A further request while one runs is
        already served by it, so a flood of them pulls once rather than once
        each.
        """
        if not self._one_at_a_time.acquire(blocking=False):
            return
        try:
            self._take_in()
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
        """What that deck's page says, or nothing while no deck carries the slug."""
        deck = self.store.get(slug)
        if deck is None:
            return None
        return DeckPage(
            slug=deck.slug,
            title=deck.title,
            source=self.source_address(),
            commit=deck.commit[:_SHORT_COMMIT],
            built=deck.active_build is not None,
        )

    def built_talk(self, slug: str) -> Path | None:
        """The directory the deck's talk is delivered from, while one is built."""
        deck = self.store.get(slug)
        return None if deck is None else deck.active_build

    def source_address(self) -> str | None:
        """The git address an empty list names, while one is configured."""
        source = self.sources.configured()
        return None if source is None else source.url

    def _take_in(self) -> None:
        """Store every folder that carries both a manifest and slides.

        The folder name becomes the slug, so a changed title reaches no address.
        A folder the source has stopped carrying is marked as removed rather
        than deleted, and a folder that comes back under its old name loses that
        mark, so it is the deck it was. A source nobody could read carries no
        such news, and leaves every deck where it is.
        """
        source = self.sources.configured()
        if source is None:
            return
        carried = self.folders.folders(source)
        if carried is None:
            return
        for folder in carried:
            if folder.title is None or SLIDES_FILE not in folder.file_names:
                continue
            self.store.put(
                Deck(
                    slug=folder.name,
                    title=folder.title,
                    changed_at=folder.changed_at,
                    owner_id=source.owner_id,
                    commit=folder.commit,
                    active_build=None,
                ),
            )
        self.store.mark_removed_except(
            present=frozenset(folder.name for folder in carried),
            at=self.clock.now(),
        )
