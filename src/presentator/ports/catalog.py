"""The lobby and the build talk to these capabilities.

The first adapter that fills them is a later slice.
"""

from typing import Protocol

from presentator.contracts.models import Deck, Source


class DeckStore(Protocol):
    """Decks are keyed by folder slug, so a title change cannot fork the talk."""

    def get(self, slug: str) -> Deck | None:
        """The deck page and the start route look up one talk by slug."""
        ...  # pragma: no cover  # Protocol body is never executed

    def put(self, deck: Deck) -> None:
        """Intake upserts on slug so a deleted-and-pushed folder is the same talk."""
        ...  # pragma: no cover  # Protocol body is never executed

    def list_newest_first(self) -> tuple[Deck, ...]:
        """The lobby sorts by recency in the store, so callers do not re-decide it."""
        ...  # pragma: no cover  # Protocol body is never executed

    def remove(self, slug: str) -> None:
        """A folder that vanished from git leaves the lobby."""
        ...  # pragma: no cover  # Protocol body is never executed


class SourceStore(Protocol):
    """Every source has an owner from the first row (ADR 0010)."""

    def get(self, source_id: str) -> Source | None:
        """A deck names its source by id, never by URL."""
        ...  # pragma: no cover  # Protocol body is never executed

    def put(self, source: Source) -> None:
        """The first source is configuration written as a row, not a special case."""
        ...  # pragma: no cover  # Protocol body is never executed

    def list(self) -> tuple[Source, ...]:
        """Settings lists sources; the empty lobby still asks the same store."""
        ...  # pragma: no cover  # Protocol body is never executed


class BuildRunner(Protocol):
    """Building is a port so the first implementation can be a later slice."""

    def run(self, deck: Deck) -> str:
        """Return the path of the built talk so the deck row can point at it."""
        ...  # pragma: no cover  # Protocol body is never executed
