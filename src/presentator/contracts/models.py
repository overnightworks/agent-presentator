"""The identity types every layer of the login speaks."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Role(StrEnum):
    """An instance has only these two roles (ADR 0011)."""

    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    """Who may log in."""

    id: str
    username: str
    role: Role

    @property
    def is_admin(self) -> bool:
        """Only an admin reaches Settings and the instance defaults (ADR 0011)."""
        return self.role is Role.ADMIN


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """A stored sign-in: who it belongs to and the instants it stands between.

    Whether it still stands is `webauth`'s `admit_session`, not a method here
    (ADR 0003).
    """

    id: str
    user: User
    created_at: datetime
    expires_at: datetime
