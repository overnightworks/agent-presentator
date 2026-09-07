"""Where decks come from and where they are kept: git, TOML, and SQLite.

`gitmirror` (ADR 0010) owns talking to the remote; this module translates
between its vocabulary and the product's, and holds the deck table (ADR 0006).
"""

import json
import logging
import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

from gitmirror.mirror import GitMirror
from gitmirror.model import (
    CredentialReference,
    CredentialResolver,
    GitSource,
    Revision,
)
from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.decks import (
    MANIFEST_FILE,
    Deck,
    DeckFolder,
    Source,
)
from presentator.ports.identity import UserStore

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS decks (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id),
    removed_at TEXT
);
"""
_TITLE_KEY: Final = "title"
_UNREADABLE_SOURCE: Final = "source %s cannot be read: %s"
_UNREADABLE_MANIFEST: Final = "folder %s is not listed: %s"

_log = logging.getLogger(__name__)


class MalformedManifestError(ValueError):
    """A manifest that cannot be read, or that names no title."""


def create_deck_tables(database: Path) -> None:
    """Make the deck table exist."""
    apply_schema(database, _SCHEMA)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfiguredSource:
    """The single source an installation carries in its configuration.

    Nobody creates it at a surface, so the account that set the instance up
    owns it; from the sources page on, a source belongs to whoever added it.
    """

    url: str | None
    ref: str
    credential_reference: str | None
    accounts: UserStore

    def configured(self) -> Source | None:
        """The configured source and its owner, once both exist."""
        owner = self.accounts.first_admin()
        if self.url is None or owner is None:
            return None
        return Source(
            url=self.url,
            ref=self.ref,
            credential_reference=self.credential_reference,
            owner_id=owner.id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvironmentCredentials:
    """Resolves a credential reference to the value the environment carries.

    The configuration holds the name, so the durable record never holds a
    secret; the value is read out of the process environment at every pull.
    """

    def resolve(self, reference: CredentialReference) -> str | None:
        """The secret behind the reference, or nothing when it leads nowhere."""
        return os.environ.get(reference.name)


@dataclass(frozen=True, slots=True, kw_only=True)
class MirroredDeckFolders:
    """Reads a source's folders out of a local bare mirror of its repository."""

    mirrors: Path
    credentials: CredentialResolver
    pull_timeout: timedelta

    def folders(self, source: Source) -> tuple[DeckFolder, ...] | None:
        """Every top-level folder at the newest commit, or nothing when unreadable."""
        mirror = self._mirror(source)
        connection = mirror.connect()
        revision = connection.revision
        if revision is None:
            _log.warning(_UNREADABLE_SOURCE, source.url, connection.state)
            return None
        return tuple(
            self._folder(mirror, revision, entry.name)
            for entry in mirror.entries(revision)
            if entry.is_directory
        )

    def _folder(
        self,
        mirror: GitMirror,
        revision: Revision,
        name: str,
    ) -> DeckFolder:
        """The folder as it stands, without a title when its manifest is unreadable.

        A folder nobody can read a title from is still a folder that is there,
        so it must not be reported as gone; the row that says why it carries no
        title belongs to the build states.
        """
        file_names = frozenset(
            entry.name
            for entry in mirror.entries(revision, inside=name)
            if not entry.is_directory
        )
        return DeckFolder(
            name=name,
            file_names=file_names,
            title=self._read_title(mirror, revision, name, file_names),
            changed_at=mirror.last_changed_at(revision, name),
        )

    def _read_title(
        self,
        mirror: GitMirror,
        revision: Revision,
        name: str,
        file_names: frozenset[str],
    ) -> str | None:
        """What the folder's manifest names, or nothing when it names nothing."""
        if MANIFEST_FILE not in file_names:
            return None
        try:
            return _title(mirror.read(revision, f"{name}/{MANIFEST_FILE}"), folder=name)
        except MalformedManifestError as unreadable:
            _log.warning(_UNREADABLE_MANIFEST, name, unreadable)
            return None

    def _mirror(self, source: Source) -> GitMirror:
        reference = source.credential_reference
        return GitMirror(
            source=GitSource(
                url=source.url,
                ref=source.ref,
                credential=(
                    None if reference is None else CredentialReference(name=reference)
                ),
            ),
            directory=self.mirrors / _mirror_name(source),
            credentials=self.credentials,
            pull_timeout=self.pull_timeout,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SqliteDeckStore:
    """The deck table."""

    database: Path

    def put(self, deck: Deck) -> None:
        """Write the deck under its slug, so a re-pushed folder stays one deck."""
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO decks (slug, title, changed_at, owner_id)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(slug) DO UPDATE SET"
                " title = excluded.title, changed_at = excluded.changed_at,"
                " owner_id = excluded.owner_id",
                (
                    deck.slug,
                    deck.title,
                    deck.changed_at.isoformat(),
                    deck.owner_id,
                ),
            )

    def mark_removed_except(self, present: frozenset[str], *, at: datetime) -> None:
        """Mark the decks outside `present` removed, and clear the mark inside it."""
        # A set is no bind parameter, so the folders the source carries travel
        # as one JSON value rather than as SQL built per call.
        carried = json.dumps(sorted(present))
        with rows(self.database) as cursor:
            cursor.execute(
                "UPDATE decks SET removed_at = ? WHERE removed_at IS NULL"
                " AND slug NOT IN (SELECT value FROM json_each(?))",
                (at.isoformat(), carried),
            )
            cursor.execute(
                "UPDATE decks SET removed_at = NULL WHERE removed_at IS NOT NULL"
                " AND slug IN (SELECT value FROM json_each(?))",
                (carried,),
            )

    def all(self) -> tuple[Deck, ...]:
        """Read every deck row whose folder is still at its source."""
        with rows(self.database) as cursor:
            found = cursor.execute(
                "SELECT slug, title, changed_at, owner_id FROM decks"
                " WHERE removed_at IS NULL",
            ).fetchall()
        return tuple(
            Deck(
                slug=slug,
                title=title,
                changed_at=datetime.fromisoformat(changed_at),
                owner_id=owner_id,
            )
            for slug, title, changed_at, owner_id in found
        )


def _title(manifest: bytes, *, folder: str) -> str:
    try:
        named = tomllib.loads(manifest.decode()).get(_TITLE_KEY)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as unreadable:
        message = f"{folder}/{MANIFEST_FILE} is not readable TOML: {unreadable}"
        raise MalformedManifestError(message) from unreadable
    if not isinstance(named, str):
        message = f"{folder}/{MANIFEST_FILE} names no title"
        raise MalformedManifestError(message)
    return named


def _mirror_name(source: Source) -> str:
    # A URL is not a directory name, and two sources must not share a mirror.
    return f"{sha256(source.url.encode()).hexdigest()}.git"
