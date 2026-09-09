"""Mirrors any git remote read-only, for callers that never write back.

An independent package beside the product's layers (ADR 0010): it knows about
git and credentials, and nothing about presentations. When a second caller
arrives it moves to its own repository and is consumed by tag.
"""

from gitmirror.mirror import GitMirror, check_connection
from gitmirror.model import (
    Change,
    Connection,
    ConnectionCheck,
    ConnectionNextAction,
    ConnectionState,
    CredentialReference,
    CredentialResolver,
    GitSource,
    GitUnavailableError,
    InvalidCredentialError,
    MirrorError,
    Revision,
    TreeEntry,
)

__all__ = (
    "Change",
    "Connection",
    "ConnectionCheck",
    "ConnectionNextAction",
    "ConnectionState",
    "CredentialReference",
    "CredentialResolver",
    "GitMirror",
    "GitSource",
    "GitUnavailableError",
    "InvalidCredentialError",
    "MirrorError",
    "Revision",
    "TreeEntry",
    "check_connection",
)
