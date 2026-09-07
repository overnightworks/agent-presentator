"""Where decks come from and where they are kept: git, TOML, and SQLite.

`gitmirror` (ADR 0010) owns talking to the remote; this module translates
between its vocabulary and the product's, and holds the deck table (ADR 0006).
"""

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
    commit_sha TEXT NOT NULL,
    active_build TEXT
);
"""
# The build pointer is left out of the insert's update list on purpose: taking
# a deck in again must not unpresent the talk that already stands (line 16).
_PUT_DECK: Final = """
INSERT INTO decks (slug, title, changed_at, owner_id, commit_sha, active_build)
VALUES (?, ?, ?, ?, ?, NULL)
ON CONFLICT(slug) DO UPDATE SET
    title = excluded.title,
    changed_at = excluded.changed_at,
    owner_id = excluded.owner_id,
    commit_sha = excluded.commit_sha
"""
_PUT_ACTIVE_BUILD: Final = "UPDATE decks SET active_build = ? WHERE slug = ?"
_ALL_DECKS: Final = """
SELECT slug, title, changed_at, owner_id, commit_sha, active_build FROM decks
"""
_ONE_DECK: Final = """
SELECT slug, title, changed_at, owner_id, commit_sha, active_build FROM decks
WHERE slug = ?
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

    def folders(self, source: Source) -> tuple[DeckFolder, ...]:
        """Every top-level folder at the source's newest commit."""
        mirror = self._mirror(source)
        connection = mirror.connect()
        revision = connection.revision
        if revision is None:
            _log.warning(_UNREADABLE_SOURCE, source.url, connection.state)
            return ()
        read = (
            self._folder(mirror, revision, entry.name)
            for entry in mirror.entries(revision)
            if entry.is_directory
        )
        return tuple(folder for folder in read if folder is not None)

    def _folder(
        self,
        mirror: GitMirror,
        revision: Revision,
        name: str,
    ) -> DeckFolder | None:
        """The folder, or nothing when its manifest is the one thing unreadable.

        One folder nobody can read must not take the whole list with it; the
        row that says so belongs to the build states.
        """
        file_names = frozenset(
            entry.name
            for entry in mirror.entries(revision, inside=name)
            if not entry.is_directory
        )
        try:
            title = (
                _title(mirror.read(revision, f"{name}/{MANIFEST_FILE}"), folder=name)
                if MANIFEST_FILE in file_names
                else None
            )
        except MalformedManifestError as unreadable:
            _log.warning(_UNREADABLE_MANIFEST, name, unreadable)
            return None
        return DeckFolder(
            name=name,
            file_names=file_names,
            title=title,
            changed_at=mirror.last_changed_at(revision, name),
            commit=revision.commit,
        )

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
                _PUT_DECK,
                (
                    deck.slug,
                    deck.title,
                    deck.changed_at.isoformat(),
                    deck.owner_id,
                    deck.commit,
                ),
            )

    def put_active_build(self, slug: str, *, directory: Path) -> None:
        """Point the deck at the directory its talk is delivered from."""
        with rows(self.database) as cursor:
            cursor.execute(_PUT_ACTIVE_BUILD, (str(directory), slug))

    def get(self, slug: str) -> Deck | None:
        """Read the one deck row that slug names."""
        with rows(self.database) as cursor:
            found = cursor.execute(_ONE_DECK, (slug,)).fetchone()
        return None if found is None else _deck(found)

    def all(self) -> tuple[Deck, ...]:
        """Read every deck row."""
        with rows(self.database) as cursor:
            found = cursor.execute(_ALL_DECKS).fetchall()
        return tuple(_deck(row) for row in found)


def _deck(row: tuple[str, str, str, str, str, str | None]) -> Deck:
    slug, title, changed_at, owner_id, commit, active_build = row
    return Deck(
        slug=slug,
        title=title,
        changed_at=datetime.fromisoformat(changed_at),
        owner_id=owner_id,
        commit=commit,
        active_build=None if active_build is None else Path(active_build),
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
