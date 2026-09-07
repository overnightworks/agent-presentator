"""What a deck and its source are, everywhere in this product (ADR 0005)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

MANIFEST_FILE: Final = "deck.toml"
SLIDES_FILE: Final = "slides.md"


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """A git repository decks are mirrored from, holding no secret value."""

    url: str
    ref: str
    credential_reference: str | None
    owner_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckFolder:
    """A folder of a mirrored source as it stands at one commit."""

    name: str
    file_names: frozenset[str]
    title: str | None
    changed_at: datetime
    commit: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Deck:
    """A talk. The folder name is the identity; the title only shows."""

    slug: str
    title: str
    changed_at: datetime
    owner_id: str
    commit: str
    active_build: Path | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedDeck:
    """One row of the deck list, with the age the row shows."""

    slug: str
    title: str
    age: timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckPage:
    """What a deck's own page says about it before anyone speaks."""

    slug: str
    title: str
    source: str | None
    commit: str
    built: bool
