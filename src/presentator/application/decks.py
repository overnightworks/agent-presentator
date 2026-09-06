"""Taking decks in from a source, and the list a person reads.

Which folder counts as a deck, and in which order the decks are shown, is
decided here; git, TOML, and SQL stay outside (ADR 0001, ADR 0005).
"""

from dataclasses import dataclass

from presentator.contracts.decks import SLIDES_FILE, Deck, ListedDeck
from presentator.ports.clock import Clock
from presentator.ports.decks import DeckFolders, DeckStore, SourceStore


@dataclass(frozen=True, slots=True, kw_only=True)
class Decks:
    """Every use case the lobby has around the talks it knows."""

    sources: SourceStore
    folders: DeckFolders
    store: DeckStore
    clock: Clock

    def refreshed_list(self) -> tuple[ListedDeck, ...]:
        """Take in what the source carries now, then list it, newest changed first.

        Pulling on the way to the list is what makes a pushed folder appear
        without any further action; polling and the webhook widen that later.
        """
        self._take_in()
        return self._listed()

    def source_address(self) -> str | None:
        """The git address an empty list names, while one is configured."""
        source = self.sources.configured()
        return None if source is None else source.url

    def _take_in(self) -> None:
        """Store every folder that carries both a manifest and slides.

        The folder name becomes the slug, so a changed title reaches no address.
        """
        source = self.sources.configured()
        if source is None:
            return
        for folder in self.folders.folders(source):
            if folder.title is None or SLIDES_FILE not in folder.file_names:
                continue
            self.store.put(
                Deck(
                    slug=folder.name,
                    title=folder.title,
                    changed_at=folder.changed_at,
                    owner_id=source.owner_id,
                ),
            )

    def _listed(self) -> tuple[ListedDeck, ...]:
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
