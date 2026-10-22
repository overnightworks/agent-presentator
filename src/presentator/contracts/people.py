"""What erasing a person reports: the kinds of row they owned and how many."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PersonKind(StrEnum):
    """One kind of thing a person owns, as the erasure counts it."""

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
