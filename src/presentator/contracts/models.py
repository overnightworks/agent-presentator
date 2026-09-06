"""Identity, catalog, and build-state types every later M0 slice shares.

A deck's slug is its folder name, so a title change cannot move the talk.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class Role(StrEnum):
    """An instance has only these two roles (ADR 0011)."""

    ADMIN = "admin"
    USER = "user"


class BuildState(StrEnum):
    """A build is one of these four, and the set does not grow."""

    PENDING = "pending"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InvalidDeckSlugError(ValueError):
    """A slug must be a single folder name, not a path or a special directory."""


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    """Who may log in; the password hash stays in the store, not on this record."""

    id: str
    username: str
    role: Role


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """Idle lifetime is a pure function of last_seen.

    A store cannot invent a different rule.
    """

    id: str
    user_id: str
    last_seen: datetime

    def is_alive(self, at: datetime, idle_window: timedelta) -> bool:
        """Alive strictly inside the window, so the boundary itself is expired."""
        return at - self.last_seen < idle_window


@dataclass(frozen=True, slots=True, kw_only=True)
class Deck:
    """The slug is the folder name; title is display only."""

    slug: str
    title: str
    owner_id: str
    source_id: str
    changed_at: datetime
    active_build_path: str | None = None
    last_error: str | None = None

    @staticmethod
    def slug_from(directory_name: str) -> str:
        """Reject names that cannot be a single folder on disk."""
        rejected = (
            directory_name in {"", ".", ".."}
            or "/" in directory_name
            or "\0" in directory_name
        )
        if rejected:
            message = "deck slug must be a folder name"
            raise InvalidDeckSlugError(message)
        return directory_name


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """Owner is required from the first row (ADR 0011)."""

    id: str
    owner_id: str
    url: str
    ref: str
    credential_ref: str | None = None
