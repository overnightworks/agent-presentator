"""Where decks come from and where they are kept: git, TOML, and SQLite.

`gitmirror` (ADR 0010) owns talking to the remote; this module translates
between its vocabulary and the product's, and holds the deck table (ADR 0006).
"""

import json
import logging
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

from gitmirror.mirror import GitMirror, check_connection
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
    RECENT_SOURCE_RUNS,
    AccessKind,
    Build,
    BuildAttempt,
    BuildOutcome,
    ConnectionCheckResult,
    Deck,
    DeckFolder,
    SecretLocation,
    Source,
    SourcePoll,
    SourceRun,
    SourceRunFailure,
    SourceRunOutcome,
    SourceWrite,
    access_kind_of,
    local_mount_path_of,
)
from presentator.ports.decks import LocalMount
from presentator.ports.identity import IdentifierFactory

# A deck's source is nullable because a file written before sources were rows
# gains the column with nothing in it; every deck taken in since names the
# source that carried it.
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE,
    ref TEXT NOT NULL,
    credential_reference TEXT,
    encrypted_secret BLOB,
    hook_secret_hash BLOB,
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
_HOOK_HASH_COLUMN: Final = "hook_secret_hash"
_ADD_HOOK_HASH_TO_SOURCES: Final = (
    f"ALTER TABLE sources ADD COLUMN {_HOOK_HASH_COLUMN} BLOB"
)
_ADD_SOURCE: Final = """
INSERT INTO sources (id, name, url, ref, encrypted_secret, hook_secret_hash, owner_id)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""
_HOOK_HASH_BY_NAME: Final = "SELECT hook_secret_hash FROM sources WHERE name = ?"
_ALL_SOURCES: Final = """
SELECT id, name, url, ref, credential_reference, encrypted_secret, owner_id
FROM sources
"""
# The ciphertext is written on its own: everything else about a source is what
# the operator typed, while this is the one value the instance key can open.
# A leftover environment reference is cleared so the row no longer names a
# place this instance does not read.
_PUT_ENCRYPTED_VALUE: Final = """
UPDATE sources
SET encrypted_secret = ?, credential_reference = NULL
WHERE id = ?
"""
_PUT_HOOK_HASH: Final = "UPDATE sources SET hook_secret_hash = ? WHERE name = ?"
_ONE_ROW: Final = 1
# What a pull needs and nothing else: the ciphertext this instance can open.
_SOURCE_ANCHOR: Final = "SELECT encrypted_secret FROM sources WHERE id = ?"
_SourceRow = tuple[str, str, str, str, str | None, bytes | None, str]
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
_OUTSIDE_MOUNT: Final = (
    "source %s no longer resolves under the local mount, treated as unreadable"
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
_RECENT_SOURCE_RUNS: Final = """
SELECT source_id, at, outcome, commit_sha, reason
FROM source_runs
WHERE source_id = ?
ORDER BY id DESC
LIMIT ?
"""
_PRUNE_OLDER_RUNS: Final = """
DELETE FROM source_runs
WHERE source_id = ?
  AND id NOT IN (
    SELECT id FROM source_runs
    WHERE source_id = ?
    ORDER BY id DESC
    LIMIT ?
  )
"""
_SourceRunRow = tuple[str, str, str, str | None, str | None]
# `gitmirror`'s own words never reach a `SourceRun`; a connection that carries
# no revision is always one of these four states, never `READY`.
_FAILURE_BY_CONNECTION_STATE: Final[dict[ConnectionState, SourceRunFailure]] = {
    ConnectionState.CREDENTIAL_UNRESOLVABLE: SourceRunFailure.CREDENTIAL_UNRESOLVABLE,
    ConnectionState.REFUSED: SourceRunFailure.REFUSED,
    ConnectionState.FAILED: SourceRunFailure.FAILED,
    ConnectionState.UNREACHABLE: SourceRunFailure.UNREACHABLE,
}

_log = logging.getLogger(__name__)


class MalformedManifestError(ValueError):
    """A manifest that cannot be read, or that names no title."""


def create_deck_tables(database: Path) -> None:
    """Make the source and deck tables exist, keeping what an older file holds.

    A file written before sources were rows keeps its decks and gains the
    column naming theirs, one written before a build kept what it attempted
    gains those columns, one written before a source could hold its own
    secret gains that column, and one written before a source carried a
    webhook-secret hash gains that column, rather than being replaced by an
    empty file.
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
        _add_missing(
            cursor,
            shape=_SOURCE_COLUMNS,
            column=_HOOK_HASH_COLUMN,
            add=_ADD_HOOK_HASH_TO_SOURCES,
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
class SqliteSourceStore:
    """The sources table: one row per repository this instance mirrors."""

    database: Path
    identifiers: IdentifierFactory
    box: SecretBox

    def add(self, write: SourceWrite) -> Source | None:
        """Write a new source with its secrets, leaving every existing row alone.

        A name or URL this table already carries is IntegrityError rather than
        an overwrite: a second source with the same identity is not stored.
        """
        identifier = self.identifiers.new_id()
        try:
            with rows(self.database) as cursor:
                cursor.execute(
                    _ADD_SOURCE,
                    (
                        identifier,
                        write.name,
                        write.url,
                        write.ref,
                        self.box.encrypt(write.access_secret),
                        write.hook_secret_hash,
                        write.owner_id,
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return Source(
            id=identifier,
            name=write.name,
            url=write.url,
            ref=write.ref,
            secret_location=SecretLocation.STORED,
            owner_id=write.owner_id,
        )

    def hook_secret_hash(self, name: str) -> bytes | None:
        """The stored hash of that source's webhook secret, if this name exists."""
        with rows(self.database) as cursor:
            found = cursor.execute(_HOOK_HASH_BY_NAME, (name,)).fetchone()
        if found is None:
            return None
        digest: bytes | None = found[0]
        return digest

    def put_credential(self, source_id: str, secret: str) -> None:
        """Keep that source's read-only secret in its row, encrypted.

        The value is written down in the one form this instance can open
        again, so what stands in the file is of no use to whoever reads the
        file without the instance key. A leftover environment-variable name
        on the row is cleared: this instance no longer reads one.
        """
        with rows(self.database) as cursor:
            cursor.execute(_PUT_ENCRYPTED_VALUE, (self.box.encrypt(secret), source_id))

    def put_hook_secret_hash(self, name: str, digest: bytes) -> bool:
        """Replace that source's webhook-secret hash, or nothing when it is missing."""
        with rows(self.database) as cursor:
            written = cursor.execute(_PUT_HOOK_HASH, (digest, name))
            return written.rowcount == _ONE_ROW

    def all(self) -> tuple[Source, ...]:
        """Read every source this instance mirrors decks from."""
        with rows(self.database) as cursor:
            found = cursor.execute(_ALL_SOURCES).fetchall()
        return tuple(_source(row) for row in found)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceCredentials:
    """Answers with the secret the source's own row stores, at the pull.

    The one place a secret stands is the ciphertext column. A row that still
    names an environment variable, and a row that names none, both answer
    with nothing until a secret is written into that column.
    """

    database: Path
    box: SecretBox

    def resolve(self, reference: CredentialReference) -> str | None:
        """The secret of the source that reference anchors, or nothing.

        Nothing is the honest answer to a source this instance no longer
        carries, to a row holding no ciphertext, and to ciphertext another
        instance key wrote; the pull that asked reads all three as a
        credential it cannot resolve.
        """
        with rows(self.database) as cursor:
            found = cursor.execute(_SOURCE_ANCHOR, (reference.name,)).fetchone()
        if found is None:
            return None
        encrypted: bytes | None = found[0]
        return None if encrypted is None else self.box.decrypt(encrypted)


@dataclass(frozen=True, slots=True, kw_only=True)
class FilesystemLocalMount:
    """Resolves a file-kind address against the real, mounted directory.

    `local_mount_path_of` already rejects an address that cannot even name a
    path; this is the one place that asks the real filesystem, because a
    symlink or a mount whose target has since changed is invisible to a
    lexical check. Both the address and the mount are resolved the way git
    itself would open them — following every symlink, refusing what does not
    exist — before either is trusted, so the answer is the real path, not the
    operator's spelling of it, and a repository that no longer stands under
    the mount is refused exactly as one that was never under it.
    """

    mount: Path

    def canonical_repository(self, address: str) -> Path | None:
        """The address's real path, resolved and confirmed under the mount."""
        path = local_mount_path_of(address)
        if path is None:
            return None
        try:
            mount = self.mount.resolve(strict=True)
            repository = Path(path).resolve(strict=True)
        except OSError:
            return None
        if repository != mount and mount not in repository.parents:
            return None
        return repository


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceMirrors:
    """The bare mirrors this instance keeps, and how one of them is opened.

    Reading a source's folders and building a deck out of one both need the
    same mirror, so where it lives and how it is named is decided once.
    """

    directory: Path
    credentials: CredentialResolver
    pull_timeout: timedelta
    local_mount: LocalMount

    def of(self, source: Source) -> GitMirror | None:
        """The mirror of that source, or nothing when it no longer resolves.

        A file-kind address that no longer resolves under the mount —
        whatever moved it — is unreadable rather than a fetch of wherever it
        now points.
        """
        url = source.url
        if access_kind_of(url) is AccessKind.FILE:
            canonical = self.local_mount.canonical_repository(url)
            if canonical is None:
                return None
            url = str(canonical)
        return GitMirror(
            source=GitSource(
                url=url,
                ref=source.ref,
                # The source's own id is the anchor, because the row is what
                # knows whether a secret stands; the resolver reads it there.
                # A leftover environment row has no ciphertext, so the
                # resolver answers with nothing and the pull is unresolvable
                # rather than an anonymous fetch that would look like success.
                credential=CredentialReference(name=source.id),
            ),
            # A URL is not a directory name, and two sources must not share a
            # mirror.
            directory=self.directory / f"{sha256(source.url.encode()).hexdigest()}.git",
            credentials=self.credentials,
            pull_timeout=self.pull_timeout,
        )


# The only access kinds the checker knows how to probe; anything else — no
# scheme it recognises, or SSH, which the form does not offer yet — is
# refused before a single argument reaches git, never handed to it on the
# chance a probe might make sense of it.
_PROBED_ACCESS_KINDS: Final = frozenset({AccessKind.HTTPS, AccessKind.FILE})


@dataclass(frozen=True, slots=True, kw_only=True)
class MirroredConnectionChecker:
    """Probes a form's own URL and secret through `ls-remote`, storing nothing."""

    check_timeout: timedelta
    local_mount: LocalMount

    def check(self, *, url: str, ref: str, secret: str) -> ConnectionCheckResult:
        """No failure and the head commit when reachable, or which failure and why.

        A file-kind address is resolved against the real mount first, the
        same way a stored source's own mirror is: one outside it is refused
        without ever reaching `ls-remote`, because no host answered no and no
        host failed to answer — the mount itself declined to open it. An
        access kind the checker does not probe is refused the same way.
        """
        kind = access_kind_of(url)
        if kind not in _PROBED_ACCESS_KINDS:
            return ConnectionCheckResult(
                failure=SourceRunFailure.REFUSED, commit=None, detail=None
            )
        resolved = url
        if kind is AccessKind.FILE:
            canonical = self.local_mount.canonical_repository(url)
            if canonical is None:
                return ConnectionCheckResult(
                    failure=SourceRunFailure.REFUSED,
                    commit=None,
                    detail=None,
                )
            resolved = str(canonical)
        probed = check_connection(
            url=resolved,
            ref=ref,
            secret=secret or None,
            timeout=self.check_timeout,
        )
        if probed.state is ConnectionState.READY:
            return ConnectionCheckResult(
                failure=None,
                commit=probed.commit,
                detail=None,
            )
        return ConnectionCheckResult(
            failure=_FAILURE_BY_CONNECTION_STATE[probed.state],
            commit=None,
            detail=probed.detail,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MirroredDeckFolders:
    """Reads a source's folders out of a local bare mirror of its repository."""

    mirrors: SourceMirrors

    def folders(self, source: Source) -> SourcePoll:
        """Poll for every top-level folder at the newest commit, or why not."""
        mirror = self.mirrors.of(source)
        if mirror is None:
            _log.warning(_OUTSIDE_MOUNT, source.url)
            return SourcePoll(
                folders=None,
                commit=None,
                failure=SourceRunFailure.FAILED,
            )
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
    """The `source_runs` table: the newest polls of each source, bounded."""

    database: Path

    def record(self, run: SourceRun) -> None:
        """Add the run and drop older ones of that source past the bound."""
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
            cursor.execute(
                _PRUNE_OLDER_RUNS,
                (run.source_id, run.source_id, RECENT_SOURCE_RUNS),
            )

    def newest(self, source_id: str) -> SourceRun | None:
        """Read that source's newest row, or nothing while it carries none."""
        with rows(self.database) as cursor:
            found = cursor.execute(_NEWEST_SOURCE_RUN, (source_id,)).fetchone()
        return None if found is None else _source_run(found)

    def recent(self, source_id: str) -> tuple[SourceRun, ...]:
        """Read that source's newest rows, newest first, no more than the bound."""
        with rows(self.database) as cursor:
            found = cursor.execute(
                _RECENT_SOURCE_RUNS,
                (source_id, RECENT_SOURCE_RUNS),
            ).fetchall()
        return tuple(_source_run(row) for row in found)


def _source(row: _SourceRow) -> Source:
    identifier, name, url, ref, _, encrypted, owner_id = row
    return Source(
        id=identifier,
        name=name,
        url=url,
        ref=ref,
        secret_location=_where_the_secret_stands(encrypted),
        owner_id=owner_id,
    )


def _where_the_secret_stands(encrypted: bytes | None) -> SecretLocation | None:
    """Stored when the row carries ciphertext; nothing while it does not.

    A leftover environment-variable name is not a secret this instance can
    hand to git, so it does not count as a place the secret stands.
    """
    if encrypted is not None:
        return SecretLocation.STORED
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
