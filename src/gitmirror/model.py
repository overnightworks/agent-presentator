"""What a caller hands in and gets back; no git command appears here."""

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol


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
    REFUSED = "refused"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


class ConnectionNextAction(StrEnum):
    """The one action a caller can take after each connection state."""

    NONE = "none"
    RESOLVE_CREDENTIAL = "resolve-credential"
    REPAIR_ACCESS = "repair-access"
    CHECK_REACHABILITY = "check-reachability"
    INSPECT_FAILURE = "inspect-failure"


_CONNECTION_NEXT_ACTIONS: Final[Mapping[ConnectionState, ConnectionNextAction]] = (
    MappingProxyType(
        {
            ConnectionState.READY: ConnectionNextAction.NONE,
            ConnectionState.CREDENTIAL_UNRESOLVABLE: (
                ConnectionNextAction.RESOLVE_CREDENTIAL
            ),
            ConnectionState.REFUSED: ConnectionNextAction.REPAIR_ACCESS,
            ConnectionState.UNREACHABLE: ConnectionNextAction.CHECK_REACHABILITY,
            ConnectionState.FAILED: ConnectionNextAction.INSPECT_FAILURE,
        }
    )
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Connection:
    """What a pull found: the state, and the revision when the state is ready."""

    state: ConnectionState
    revision: Revision | None

    @property
    def next_action(self) -> ConnectionNextAction:
        """The action this connection state calls for."""
        return _CONNECTION_NEXT_ACTIONS[self.state]


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionCheck:
    """What a bounded `ls-remote` probe found, without ever writing a mirror to disk.

    Ready's own answer is the ref's full head commit; every other state
    carries git's own sanitised first line instead, or nothing when the
    probe never reached git at all.
    """

    state: ConnectionState
    commit: str | None
    detail: str | None

    @property
    def next_action(self) -> ConnectionNextAction:
        """The action this connection state calls for."""
        return _CONNECTION_NEXT_ACTIONS[self.state]


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """The commit that last touched one path, and when it did."""

    commit: str
    at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeEntry:
    """One name in a tree at a commit, and whether it holds further names."""

    name: str
    is_directory: bool


class CredentialResolver(Protocol):
    """Turns a reference into the secret behind it, at the moment it is needed.

    Resolving at the pull leaves rotation to whoever holds the value, rather
    than to the mirror.
    """

    @abstractmethod
    def resolve(self, reference: CredentialReference) -> str | None:
        """Return the secret, or nothing when the reference leads nowhere."""


class GitUnavailableError(RuntimeError):
    """The machine has no `git`, which is a broken install, not a bad source."""


class MirrorError(RuntimeError):
    """A git command that had to succeed did not."""


class InvalidCredentialError(ValueError):
    """A resolved secret carries a control character git's line protocol cannot.

    The credential helper answers git one line per field; a raw CR or LF
    inside the secret would forge a second line before it ever reaches a
    shell escaping concern, so this is checked at the boundary where a
    resolved secret enters the mirror, whatever resolver it came from.
    """
