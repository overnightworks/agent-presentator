"""What a caller hands in and gets back; no git command appears here."""

from abc import abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialReference:
    """Where the read-only secret is found, never the secret itself.

    Vocabulary from atelier-2 ADR 0017: a source holds one credential anchor,
    and the durable record stores a reference rather than a value.
    """

    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GitSource:
    """A remote and the one ref this mirror follows."""

    url: str
    ref: str
    credential: CredentialReference | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Revision:
    """The commit that arrived, and the ref it arrived on."""

    ref: str
    commit: str


class ConnectionState(StrEnum):
    """The answer to "can this source be read", as a value, not a message."""

    READY = "ready"
    CREDENTIAL_UNRESOLVABLE = "credential-unresolvable"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True, kw_only=True)
class Connection:
    """What a pull found: the state, and the revision when the state is ready."""

    state: ConnectionState
    revision: Revision | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeEntry:
    """One name in a tree at a commit, and whether it holds further names."""

    name: str
    is_directory: bool


class CredentialResolver(Protocol):
    """Turns a reference into the secret behind it, at the moment it is needed.

    Resolving late is what lets a secret rotate without restarting the caller.
    """

    @abstractmethod
    def resolve(self, reference: CredentialReference) -> str | None:
        """Return the secret, or nothing when the reference leads nowhere."""


class GitUnavailableError(RuntimeError):
    """The machine has no `git`, which is a broken install, not a bad source."""


class MirrorError(RuntimeError):
    """A git command that had to succeed did not."""
