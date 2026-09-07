"""The identity types every layer of the login speaks."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class FirstStartClosedError(RuntimeError):
    """An instance with an account creates the next one through an admin only."""


class Role(StrEnum):
    """An instance has only these two roles (ADR 0011)."""

    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    """Who may log in; the password hash never rides along on this record."""

    id: str
    username: str
    role: Role

    @property
    def is_admin(self) -> bool:
        """Only an admin reaches Settings and the instance defaults (ADR 0011)."""
        return self.role is Role.ADMIN


@dataclass(frozen=True, slots=True, kw_only=True)
class Credentials:
    """A user beside the hash a typed password is checked against."""

    user: User
    password_hash: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """Idle lifetime is a pure function of last_seen.

    A store cannot invent a different rule.
    """

    id: str
    user_id: str
    last_seen: datetime

    def is_alive(self, *, at: datetime, idle_window: timedelta) -> bool:
        """Alive strictly inside the window, so the boundary itself is expired."""
        return at - self.last_seen < idle_window
