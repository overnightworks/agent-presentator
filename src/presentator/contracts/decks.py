"""What a deck and its source are, everywhere in this product (ADR 0005)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

MANIFEST_FILE: Final = "deck.toml"
SLIDES_FILE: Final = "slides.md"
DECK_PATH: Final = "/deck"


def talk_address(slug: str) -> str:
    """Where a deck's built talk stands, and what it is built against.

    A build writes its own asset links against this address, so the string the
    build is given and the address the lobby delivers it at are the one value.
    """
    return f"{DECK_PATH}/{slug}/"


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
class Artefacts:
    """What one run of the build left on disk: a talk to serve, and its PDF."""

    directory: Path
    pdf: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class Build:
    """The talk a deck delivers, the commit it was built from, and when.

    The four travel together because they switch together: a page that named a
    build time of one commit beside the directory of another would lie about
    what is on the screen.
    """

    directory: Path
    pdf: Path
    commit: str
    built_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Deck:
    """A talk. The folder name is the identity; the title only shows."""

    slug: str
    title: str
    changed_at: datetime
    owner_id: str
    commit: str
    build: Build | None


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
    built_ago: timedelta | None
