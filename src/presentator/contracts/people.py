"""What erasing or exporting a person reports: the kinds of row they own."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from presentator.contracts.decks import SourceRunFailure, SourceRunOutcome
from presentator.contracts.models import User
from presentator.contracts.preferences import PersonPreferences


class PersonKind(StrEnum):
    """One kind of thing a person owns, as the erasure counts it.

    The last four are the secrets an export withholds, each under its own word.
    """

    ACCOUNT = "account"
    SESSIONS = "sessions"
    PREFERENCES = "preferences"
    KEY_DRAFT = "key-draft"
    SOURCES = "sources"
    DECKS = "decks"
    RUNS = "runs"
    MIRRORS = "mirrors"
    BUILDS = "builds"
    RUNNING_WORK = "running-work"
    SOURCE_CREDENTIAL = "source-credential"
    HOOK_SECRET_HASH = "hook-secret-hash"  # noqa: S105 - a kind word, not a password
    KEY_DRAFT_PRIVATE_KEY = "key-draft-private-key"
    REFRESH_TOKEN = "refresh-token"  # noqa: S105 - a kind word, not a password


@dataclass(frozen=True, slots=True, kw_only=True)
class KindCount:
    """How many of one kind a person owns."""

    kind: PersonKind
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonErased:
    """The counts of what was removed, or of what a dry run would remove."""

    kinds: tuple[KindCount, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonBusy:
    """Work is running that the erasure waits for; nothing was deleted."""

    running: tuple[KindCount, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonDiskLeftover:
    """A mirror or build of this source survived; the source and the person remain.

    A repeat run finishes the removal.
    """

    source: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionTimes:
    """When one sign-in began and ends; nothing that could resume it."""

    created_at: datetime
    expires_at: datetime


PersonErasure = PersonErased | PersonBusy | PersonDiskLeftover


class WithheldReason(StrEnum):
    """Why an export leaves a kind out."""

    SECRET = "secret"  # noqa: S105 - a kind word, not a password


@dataclass(frozen=True, slots=True, kw_only=True)
class Withheld:
    """A kind the export counts but does not show."""

    kind: PersonKind
    reason: WithheldReason
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyDraftExport:
    """The unbound deploy key a person minted; its private half is withheld.

    A draft that no longer opens has no public key and no creation time.
    """

    public_key: str | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckExport:
    """A talk of the person's: its folder name, title, and what was built."""

    slug: str
    title: str
    changed_at: datetime
    commit: str
    built_commit: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunExport:
    """One poll of a source."""

    at: datetime
    outcome: SourceRunOutcome
    commit: str | None
    reason: SourceRunFailure | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceExport:
    """A source's address, public key, decks and runs; never its credential."""

    name: str
    url: str
    ref: str
    public_key: str | None
    decks: tuple[DeckExport, ...]
    runs: tuple[RunExport, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonData:
    """Everything shown of a person, by kind; a kind they lack is empty."""

    account: User | None
    sessions: tuple[SessionTimes, ...]
    preferences: PersonPreferences | None
    key_draft: KeyDraftExport | None
    sources: tuple[SourceExport, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonExport:
    """A person's data and the kinds left out of it; files stay empty."""

    data: PersonData
    withheld: tuple[Withheld, ...]
