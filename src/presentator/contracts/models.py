"""The identity types every layer of the login speaks."""

from dataclasses import dataclass
from datetime import datetime
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
class Account:
    """The stored account the login machinery reads, hash included."""

    id: str
    username: str
    role: Role
    password_hash: str
    is_active: bool = True

    def as_user(self) -> User:
        """The person this account is, without the hash."""
        return User(id=self.id, username=self.username, role=self.role)


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """A live row the idle-window policy reads: identity, origin, last_seen.

    Whether it still stands is the login library's idle-window policy, not a
    method here (ADR 0003).
    """

    id: str
    user_id: str
    last_seen: datetime
    ip_address: str = ""
    user_agent: str = ""
