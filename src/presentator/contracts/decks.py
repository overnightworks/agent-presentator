"""What a deck and its source are, everywhere in this product (ADR 0005)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

MANIFEST_FILE: Final = "deck.toml"
SLIDES_FILE: Final = "slides.md"
DECK_PATH: Final = "/deck"
# What a failed build may say on a page: the end of what its toolchain printed,
# where the reason stands. A deck's own build can print without limit, and a
# page is read by a person shortly before they speak.
FAILURE_TEXT_LIMIT: Final = 2000


def bounded_failure(said: str) -> str:
    """The last of what a build said, short enough for a page to carry."""
    return said[-FAILURE_TEXT_LIMIT:]


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


class AccessKind(StrEnum):
    """How a source is read, derived from its URL's scheme and nothing else.

    HTTPS is a token; SSH and the scp form are a deploy key. The radio on the
    form has to match this, and a mismatch is refused rather than stored as a
    third kind.
    """

    HTTPS = "https-token"
    SSH = "ssh-deploy-key"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceWrite:
    """What the store is given to insert: the row, the access value, and the hash."""

    name: str
    url: str
    ref: str
    owner_id: str
    access_secret: str
    hook_secret_hash: bytes


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
class BuildFailure:
    """That a build produced nothing, and what its toolchain said about it.

    The words are the toolchain's own, bounded by `bounded_failure`, or nothing
    where none reached the server — a run given up on, or one that printed
    nothing at all.
    """

    text: str | None


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


class BuildOutcome(StrEnum):
    """How the build a deck last started ended, while it is not its talk yet."""

    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildAttempt:
    """The build a deck last started, for as long as it did not become its talk.

    A running attempt is one nothing has reported back about yet and carries no
    failure; a failed one carries what its toolchain said, or nothing where no
    words reached the server at all. The successful switch clears the record,
    so a deck carries an attempt only while there is something to say about it.
    """

    commit: str
    started_at: datetime
    outcome: BuildOutcome
    failure: str | None


class DeckState(StrEnum):
    """What the list and the deck page say a deck is, in one word.

    It is read off the talk that stands and the attempt beside it, never
    stored: two records that can each be written on their own would otherwise
    disagree about one deck.
    """

    READY = "ready"
    BUILDING = "building"
    FAILED = "failed"
    NEVER_BUILT = "never-built"


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
    attempt: BuildAttempt | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedDeck:
    """One row of the deck list, with the state and the age the row shows."""

    slug: str
    title: str
    state: DeckState
    age: timedelta


class SourceState(StrEnum):
    """What the sources list says a source is, read off its newest run.

    No run is never-fetched, a successful run is reachable, and a failed run
    is error — whatever typed reason the failure carried. The reason is not a
    word this list speaks, so a credential the instance could not resolve
    never becomes a word on the board.
    """

    REACHABLE = "reachable"
    ERROR = "error"
    NEVER_FETCHED = "never-fetched"


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedSource:
    """One row of the sources list, with the state and the age the row shows."""

    name: str
    url: str
    access: AccessKind | None
    state: SourceState
    age: timedelta | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ShownAttempt:
    """The build a deck page reports on: its commit, its age, and what broke."""

    commit: str
    ago: timedelta
    failure: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckPage:
    """What a deck's own page says about it before anyone speaks."""

    slug: str
    title: str
    source: str | None
    commit: str
    built_ago: timedelta | None
    state: DeckState
    attempt: ShownAttempt | None
