"""Where decks come from and where they are kept: git, TOML, and SQLite.

`gitmirror` (ADR 0010) owns talking to the remote; this module translates
between its vocabulary and the product's, and holds the deck table (ADR 0006).
"""

import json
import logging
import os
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

from gitmirror.mirror import GitMirror
from gitmirror.model import (
    ConnectionState,
    CredentialReference,
    CredentialResolver,
    GitSource,
    Revision,
)
from presentator.adapters.secrets import SecretBox
from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.decks import (
    MANIFEST_FILE,
    Build,
    BuildAttempt,
    BuildOutcome,
    Deck,
    DeckFolder,
    SecretLocation,
    Source,
    SourcePoll,
    SourceRun,
    SourceRunFailure,
    SourceRunOutcome,
)
from presentator.ports.identity import IdentifierFactory, UserStore

# A deck's source is nullable because a file written before sources were rows
# gains the column with nothing in it; the seed attaches those decks, and every
# deck taken in since names the source that carried it.
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE,
    ref TEXT NOT NULL,
    credential_reference TEXT,
    encrypted_secret BLOB,
    owner_id TEXT NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS decks (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id),
    source_id TEXT REFERENCES sources(id),
    commit_sha TEXT NOT NULL,
    active_build TEXT,
    pdf_export TEXT,
    built_commit TEXT,
    built_at TEXT,
    attempt_commit TEXT,
    attempt_started_at TEXT,
    attempt_outcome TEXT,
    attempt_failure TEXT,
    removed_at TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    commit_sha TEXT,
    reason TEXT
);
"""
# The row shape without a row: a table this product wrote before a column
# existed gains it in place rather than by a new file, in the order the columns
# arrived.
_DECK_COLUMNS: Final = "SELECT * FROM decks LIMIT 0"
_ADD_COLUMN: Final = "ALTER TABLE decks ADD COLUMN {column} {definition}"
_COLUMNS_ADDED_LATER: Final = (
    ("source_id", "TEXT REFERENCES sources(id)"),
    ("attempt_commit", "TEXT"),
    ("attempt_started_at", "TEXT"),
    ("attempt_outcome", "TEXT"),
    ("attempt_failure", "TEXT"),
)
_SOURCE_COLUMNS: Final = "SELECT * FROM sources LIMIT 0"
_ENCRYPTED_COLUMN: Final = "encrypted_secret"
_ADD_ENCRYPTED_TO_SOURCES: Final = (
    f"ALTER TABLE sources ADD COLUMN {_ENCRYPTED_COLUMN} BLOB"
)
# Every uniqueness the table has, not the URL alone: a configuration naming a
# new URL under the name another source already answers to is a configuration
# to correct, never a row to overwrite.
_SEED_SOURCE: Final = """
INSERT INTO sources (id, name, url, ref, credential_reference, owner_id)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT DO NOTHING
"""
_ALL_SOURCES: Final = """
SELECT id, name, url, ref, credential_reference, encrypted_secret, owner_id
FROM sources
"""
_SOURCE_BY_URL: Final = f"{_ALL_SOURCES} WHERE url = ?"
_ADOPT_ORPHANED_DECKS: Final = "UPDATE decks SET source_id = ? WHERE source_id IS NULL"
# The ciphertext is written on its own: everything else about a source is what
# the operator typed, while this is the one value the instance key can open.
_PUT_ENCRYPTED_VALUE: Final = "UPDATE sources SET encrypted_secret = ? WHERE id = ?"
# What a pull needs and nothing else: the two columns that say where this
# source's secret stands.
_SOURCE_ANCHOR: Final = """
SELECT encrypted_secret, credential_reference
FROM sources
WHERE id = ?
"""
_SourceRow = tuple[str, str, str, str, str | None, bytes | None, str]
_AnchorRow = tuple[bytes | None, str | None]
# Nothing a build wrote is touched by this statement, on purpose: taking a deck
# in again must not unpresent the talk that already stands, nor take away the
# PDF that is already downloadable (line 16). Whether the row is marked removed
# is left out too: that mark is `mark_removed_except`'s alone, cleared there
# when the folder is carried again. The row is written only where it already
# belongs to the source writing it, so a slug is never taken from its source.
_PUT_DECK: Final = """
INSERT INTO decks (slug, title, changed_at, owner_id, source_id, commit_sha)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(slug) DO UPDATE SET
    title = excluded.title,
    changed_at = excluded.changed_at,
    owner_id = excluded.owner_id,
    commit_sha = excluded.commit_sha
WHERE decks.source_id = excluded.source_id
"""
# One statement, so everything a build wrote moves at once: no reader can find
# the talk of one commit beside the PDF or the build time of another. The
# attempt goes in the same write, because a talk that stands has nothing left
# to report about how it came about.
_PUT_BUILD: Final = """
UPDATE decks
SET active_build = ?, pdf_export = ?, built_commit = ?, built_at = ?,
    attempt_commit = NULL, attempt_started_at = NULL,
    attempt_outcome = NULL, attempt_failure = NULL
WHERE slug = ?
"""
# Nothing the build wrote is touched here: a run that begins, and a run that
# failed, leave the talk that stands and its PDF where they are (line 16).
_PUT_ATTEMPT: Final = """
UPDATE decks
SET attempt_commit = ?, attempt_started_at = ?,
    attempt_outcome = ?, attempt_failure = ?
WHERE slug = ?
"""
# A deck the seed has not attached to its source yet is no deck any source
# could refresh, so it is answered for once it names one.
_ALL_DECKS: Final = """
SELECT slug, title, changed_at, owner_id, source_id, commit_sha,
       active_build, pdf_export, built_commit, built_at,
       attempt_commit, attempt_started_at, attempt_outcome, attempt_failure
FROM decks
WHERE removed_at IS NULL AND source_id IS NOT NULL
"""
_ONE_DECK: Final = f"{_ALL_DECKS} AND slug = ?"
_MARK_REMOVED: Final = """
UPDATE decks SET removed_at = ?
WHERE removed_at IS NULL AND source_id = ?
  AND slug NOT IN (SELECT value FROM json_each(?))
"""
_UNMARK_CARRIED: Final = """
UPDATE decks SET removed_at = NULL
WHERE removed_at IS NOT NULL AND source_id = ?
  AND slug IN (SELECT value FROM json_each(?))
"""
# What the upsert changed: the one row it wrote, or nothing where the slug is
# another source's.
_ONE_ROW: Final = 1
# The deck row as SQLite hands it back: what the source carries, then what the
# build wrote, then the attempt beside it — each group four values or none.
_DeckRow = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]
_TITLE_KEY: Final = "title"
_UNREADABLE_SOURCE: Final = "source %s cannot be read: %s"
_UNREADABLE_MANIFEST: Final = "folder %s has no readable title, keeps its listing: %s"
# The name and never the URL: a URL may carry userinfo, and a log line is read
# by more eyes than the store is.
_NAME_ALREADY_TAKEN: Final = (
    "the configured source is not stored: another source answers to the name %s"
)
_RECORD_SOURCE_RUN: Final = """
INSERT INTO source_runs (source_id, at, outcome, commit_sha, reason)
VALUES (?, ?, ?, ?, ?)
"""
# `id` orders runs of the same source recorded at one identical instant, which
# a frozen test clock can produce; `at` alone cannot break that tie.
_NEWEST_SOURCE_RUN: Final = """
SELECT source_id, at, outcome, commit_sha, reason
FROM source_runs
WHERE source_id = ?
ORDER BY id DESC
LIMIT 1
"""
_SourceRunRow = tuple[str, str, str, str | None, str | None]
# `gitmirror`'s own words never reach a `SourceRun`; a connection that carries
# no revision is always one of these two states, never `READY`.
_FAILURE_BY_CONNECTION_STATE: Final[dict[ConnectionState, SourceRunFailure]] = {
    ConnectionState.CREDENTIAL_UNRESOLVABLE: SourceRunFailure.CREDENTIAL_UNRESOLVABLE,
    ConnectionState.UNREACHABLE: SourceRunFailure.UNREACHABLE,
}

_log = logging.getLogger(__name__)


class MalformedManifestError(ValueError):
    """A manifest that cannot be read, or that names no title."""


def create_deck_tables(database: Path) -> None:
    """Make the source and deck tables exist, keeping what an older file holds.

    A file written before sources were rows keeps its decks and gains the
    column naming theirs, one written before a build kept what it attempted
    gains those columns, and one written before a source could hold its own
    secret gains that column, rather than being replaced by an empty file.
    """
    apply_schema(database, _SCHEMA)
    with rows(database) as cursor:
        for column, definition in _COLUMNS_ADDED_LATER:
            _add_missing(
                cursor,
                shape=_DECK_COLUMNS,
                column=column,
                add=_ADD_COLUMN.format(column=column, definition=definition),
            )
        _add_missing(
            cursor,
            shape=_SOURCE_COLUMNS,
            column=_ENCRYPTED_COLUMN,
            add=_ADD_ENCRYPTED_TO_SOURCES,
        )


def _add_missing(
    cursor: sqlite3.Cursor,
    *,
    shape: str,
    column: str,
    add: str,
) -> None:
    """Add that column where the table this instance found does not carry it."""
    cursor.execute(shape)
    if column not in {named[0] for named in cursor.description}:
        cursor.execute(add)


@dataclass(frozen=True, slots=True, kw_only=True)
class _SeededRow:
    """What the configured source writes into the table, column by column."""

    id: str
    name: str
    url: str
    ref: str
    credential_reference: str | None
    owner_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfiguredSource:
    """The single source an installation carries in its configuration.

    Nobody creates it at a surface, so the account that set the instance up
    owns it; from the sources page on, a source belongs to whoever added it.
    """

    name: str
    url: str | None
    ref: str
    credential_reference: str | None
    accounts: UserStore

    def to_be_seeded(self, *, identifier: str) -> _SeededRow | None:
        """The row this configuration asks for, once a URL and an admin exist.

        A row, not a `Source`: the environment variable's name is a column of
        the table and belongs to nobody above it.
        """
        owner = self.accounts.first_admin()
        if self.url is None or owner is None:
            return None
        return _SeededRow(
            id=identifier,
            name=self.name,
            url=self.url,
            ref=self.ref,
            credential_reference=self.credential_reference,
            owner_id=owner.id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SqliteSourceStore:
    """The sources table, and the one row the configuration still writes."""

    database: Path
    configured: ConfiguredSource
    identifiers: IdentifierFactory
    box: SecretBox

    def seed(self) -> None:
        """Write the configured source once, and adopt the decks that name none.

        The URL identifies the row, so an instance that has run before finds
        its source rather than writing a second one, and the decks a file
        written before this table carries belong to it.
        """
        asked_for = self.configured.to_be_seeded(identifier=self.identifiers.new_id())
        if asked_for is None:
            return
        with rows(self.database) as cursor:
            cursor.execute(
                _SEED_SOURCE,
                (
                    asked_for.id,
                    asked_for.name,
                    asked_for.url,
                    asked_for.ref,
                    asked_for.credential_reference,
                    asked_for.owner_id,
                ),
            )
            stored = cursor.execute(_SOURCE_BY_URL, (asked_for.url,)).fetchone()
            if stored is None:
                _log.warning(_NAME_ALREADY_TAKEN, asked_for.name)
                return
            cursor.execute(_ADOPT_ORPHANED_DECKS, (_source(stored).id,))

    def put_credential(self, source_id: str, secret: str) -> None:
        """Keep that source's read-only secret in its row, encrypted.

        The value is written down in the one form this instance can open
        again, so what stands in the file is of no use to whoever reads the
        file without the instance key.
        """
        with rows(self.database) as cursor:
            cursor.execute(_PUT_ENCRYPTED_VALUE, (self.box.encrypt(secret), source_id))

    def all(self) -> tuple[Source, ...]:
        """Read every source this instance mirrors decks from."""
        with rows(self.database) as cursor:
            found = cursor.execute(_ALL_SOURCES).fetchall()
        return tuple(_source(row) for row in found)


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
class SourceCredentials:
    """Answers with the secret the source's own row anchors, at the pull.

    One resolver for both forms a row can anchor, and the row says which:
    a source carrying ciphertext is read out of the box, and one that still
    names an environment variable is read out of the environment. Which it is
    is never guessed from what a value looks like.
    """

    database: Path
    box: SecretBox
    environment: EnvironmentCredentials

    def resolve(self, reference: CredentialReference) -> str | None:
        """The secret of the source that reference anchors, or nothing.

        Nothing is the honest answer to a source this instance no longer
        carries, to a row holding no secret at all, and to ciphertext another
        instance key wrote; the pull that asked reads all three as a
        credential it cannot resolve.
        """
        with rows(self.database) as cursor:
            found = cursor.execute(_SOURCE_ANCHOR, (reference.name,)).fetchone()
        return None if found is None else self._behind(found)

    def _behind(self, row: _AnchorRow) -> str | None:
        """What the row anchors, read where the column it carries points."""
        encrypted, variable = row
        if encrypted is not None:
            return self.box.decrypt(encrypted)
        if variable is None:
            return None
        return self.environment.resolve(CredentialReference(name=variable))


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceMirrors:
    """The bare mirrors this instance keeps, and how one of them is opened.

    Reading a source's folders and building a deck out of one both need the
    same mirror, so where it lives and how it is named is decided once.
    """

    directory: Path
    credentials: CredentialResolver
    pull_timeout: timedelta

    def of(self, source: Source) -> GitMirror:
        """The mirror of that source, whether or not it has been pulled yet."""
        return GitMirror(
            source=GitSource(
                url=source.url,
                ref=source.ref,
                # The source's own id is the anchor, because the row is what
                # knows where its secret stands; the resolver reads it there.
                credential=(
                    None
                    if source.secret_location is None
                    else CredentialReference(name=source.id)
                ),
            ),
            # A URL is not a directory name, and two sources must not share a
            # mirror.
            directory=self.directory / f"{sha256(source.url.encode()).hexdigest()}.git",
            credentials=self.credentials,
            pull_timeout=self.pull_timeout,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MirroredDeckFolders:
    """Reads a source's folders out of a local bare mirror of its repository."""

    mirrors: SourceMirrors

    def folders(self, source: Source) -> SourcePoll:
        """Poll for every top-level folder at the newest commit, or why not."""
        mirror = self.mirrors.of(source)
        connection = mirror.connect()
        revision = connection.revision
        if revision is None:
            _log.warning(_UNREADABLE_SOURCE, source.url, connection.state)
            return SourcePoll(
                folders=None,
                commit=None,
                failure=_FAILURE_BY_CONNECTION_STATE[connection.state],
            )
        return SourcePoll(
            folders=tuple(
                self._folder(mirror, revision, entry.name)
                for entry in mirror.entries(revision)
                if entry.is_directory
            ),
            commit=revision.commit,
            failure=None,
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
        # The folder's own last commit, not the source's: a push that touched
        # another folder must leave this deck where it stands (line 19).
        changed = mirror.last_change(revision, name)
        return DeckFolder(
            name=name,
            file_names=file_names,
            title=self._read_title(mirror, revision, name, file_names),
            changed_at=changed.at,
            commit=changed.commit,
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


@dataclass(frozen=True, slots=True, kw_only=True)
class SqliteDeckStore:
    """The deck table."""

    database: Path

    def put(self, deck: Deck) -> bool:
        """Write the deck under its slug, so a re-pushed folder stays one deck.

        A slug another source already carries is left alone, and the answer
        says so: nothing was written.
        """
        with rows(self.database) as cursor:
            written = cursor.execute(
                _PUT_DECK,
                (
                    deck.slug,
                    deck.title,
                    deck.changed_at.isoformat(),
                    deck.owner_id,
                    deck.source_id,
                    deck.commit,
                ),
            )
            return written.rowcount == _ONE_ROW

    def put_build(self, slug: str, build: Build) -> None:
        """Switch everything the deck says about its talk over, in one write."""
        with rows(self.database) as cursor:
            cursor.execute(
                _PUT_BUILD,
                (
                    str(build.directory),
                    str(build.pdf),
                    build.commit,
                    build.built_at.isoformat(),
                    slug,
                ),
            )

    def put_attempt(self, slug: str, attempt: BuildAttempt) -> None:
        """Write down the build this deck last started, and how far it got."""
        with rows(self.database) as cursor:
            cursor.execute(
                _PUT_ATTEMPT,
                (
                    attempt.commit,
                    attempt.started_at.isoformat(),
                    attempt.outcome.value,
                    attempt.failure,
                    slug,
                ),
            )

    def get(self, slug: str) -> Deck | None:
        """Read the one deck row that slug names, while its folder is still there."""
        with rows(self.database) as cursor:
            found = cursor.execute(_ONE_DECK, (slug,)).fetchone()
        return None if found is None else _deck(found)

    def mark_removed_except(
        self,
        present: frozenset[str],
        *,
        source_id: str,
        at: datetime,
    ) -> None:
        """Mark that source's decks outside `present` removed, clear it inside."""
        # A set is no bind parameter, so the folders the source carries travel
        # as one JSON value rather than as SQL built per call.
        carried = json.dumps(sorted(present))
        with rows(self.database) as cursor:
            cursor.execute(_MARK_REMOVED, (at.isoformat(), source_id, carried))
            cursor.execute(_UNMARK_CARRIED, (source_id, carried))

    def all(self) -> tuple[Deck, ...]:
        """Read every deck row whose folder is still at its source."""
        with rows(self.database) as cursor:
            found = cursor.execute(_ALL_DECKS).fetchall()
        return tuple(_deck(row) for row in found)


@dataclass(frozen=True, slots=True, kw_only=True)
class SqliteSourceRunStore:
    """The `source_runs` table: one row per poll, kept for every run so far."""

    database: Path

    def record(self, run: SourceRun) -> None:
        """Add the run as a new row; an older run of the same source stands."""
        with rows(self.database) as cursor:
            cursor.execute(
                _RECORD_SOURCE_RUN,
                (
                    run.source_id,
                    run.at.isoformat(),
                    run.outcome.value,
                    run.commit,
                    None if run.reason is None else run.reason.value,
                ),
            )

    def newest(self, source_id: str) -> SourceRun | None:
        """Read that source's newest row, or nothing while it carries none."""
        with rows(self.database) as cursor:
            found = cursor.execute(_NEWEST_SOURCE_RUN, (source_id,)).fetchone()
        return None if found is None else _source_run(found)


def _source(row: _SourceRow) -> Source:
    identifier, name, url, ref, reference, encrypted, owner_id = row
    return Source(
        id=identifier,
        name=name,
        url=url,
        ref=ref,
        secret_location=_where_the_secret_stands(reference, encrypted),
        owner_id=owner_id,
    )


def _where_the_secret_stands(
    reference: str | None,
    encrypted: bytes | None,
) -> SecretLocation | None:
    """Which form the row anchors: the column it carries says so, not a value.

    The stored form wins where a row carries both, so a secret this instance
    was given replaces the environment variable an older configuration named
    without waiting for that configuration to go.
    """
    if encrypted is not None:
        return SecretLocation.STORED
    if reference is not None:
        return SecretLocation.ENVIRONMENT
    return None


def _deck(row: _DeckRow) -> Deck:
    slug, title, changed_at, owner_id, source_id, commit = row[:6]
    return Deck(
        slug=slug,
        title=title,
        changed_at=datetime.fromisoformat(changed_at),
        owner_id=owner_id,
        source_id=source_id,
        commit=commit,
        build=_build(*row[6:10]),
        attempt=_attempt(*row[10:]),
    )


def _build(
    directory: str | None,
    pdf: str | None,
    commit: str | None,
    built_at: str | None,
) -> Build | None:
    """What the row says its talk is, or nothing while no build has switched."""
    if directory is None or pdf is None or commit is None or built_at is None:
        return None
    return Build(
        directory=Path(directory),
        pdf=Path(pdf),
        commit=commit,
        built_at=datetime.fromisoformat(built_at),
    )


def _attempt(
    commit: str | None,
    started_at: str | None,
    outcome: str | None,
    failure: str | None,
) -> BuildAttempt | None:
    """The build the row says ran last, or nothing while none is worth telling."""
    if commit is None or started_at is None or outcome is None:
        return None
    return BuildAttempt(
        commit=commit,
        started_at=datetime.fromisoformat(started_at),
        outcome=BuildOutcome(outcome),
        failure=failure,
    )


def _source_run(row: _SourceRunRow) -> SourceRun:
    source_id, at, outcome, commit, reason = row
    return SourceRun(
        source_id=source_id,
        at=datetime.fromisoformat(at),
        outcome=SourceRunOutcome(outcome),
        commit=commit,
        reason=None if reason is None else SourceRunFailure(reason),
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
