"""What a deck and its source are, everywhere in this product (ADR 0005)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
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


class SecretLocation(StrEnum):
    """Where a source's read-only secret stands, never the secret itself.

    A row is the truth about its own secret; this says which of the two forms
    that row anchors, so nobody has to read a value to find out. The
    environment form is what an instance configured from `PRESENTATOR_SOURCE_*`
    still carries, and it goes when that configuration does.
    """

    ENVIRONMENT = "environment"
    STORED = "stored"


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """A git repository decks are mirrored from, holding no secret value."""

    id: str
    name: str
    url: str
    ref: str
    secret_location: SecretLocation | None
    owner_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckFolder:
    """A folder of a mirrored source as it stands at one commit."""

    name: str
    file_names: frozenset[str]
    title: str | None
    changed_at: datetime
    commit: str


class SourceRunOutcome(StrEnum):
    """Whether a poll of a source found its newest commit or could not.

    Closed to these two. The third word a board may show — "never fetched" —
    is not a value here: a source nobody has polled yet has no run at all, so
    that case is told apart by the absence of a `SourceRun`, not by a member
    of this enum.
    """

    SUCCESS = "success"
    FAILURE = "failure"


class SourceRunFailure(StrEnum):
    """Why a poll failed, in this product's own words rather than gitmirror's.

    A separate enum from `gitmirror.model.ConnectionState` keeps that
    package's exact wording from reaching a board by accident; the adapter,
    which already speaks both vocabularies, owns the translation between them.
    """

    CREDENTIAL_UNRESOLVABLE = "credential-unresolvable"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePoll:
    """What one poll of a source found: its folders and the commit reached.

    A source nobody could read carries no folders and no commit, only the
    reason; a source that answered carries both, even when it carries no
    folder at all.
    """

    folders: tuple[DeckFolder, ...] | None
    commit: str | None
    failure: SourceRunFailure | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRun:
    """One poll of a source, kept so a board can show that it was ever tried.

    The commit and the reason are each the other's absence: a run that reached
    the source carries the commit it found and no reason, one that did not
    carries the reason and no commit.
    """

    source_id: str
    at: datetime
    outcome: SourceRunOutcome
    commit: str | None
    reason: SourceRunFailure | None


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
    source_id: str
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
