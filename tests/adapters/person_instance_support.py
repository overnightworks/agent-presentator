"""Two people's rows over real SQLite and real mirror and build directories."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from pydantic import SecretStr

from presentator.adapters.builds import HostToolchain, SlidevBuilds
from presentator.adapters.decks import (
    FilesystemLocalMount,
    MirroredDeckFolders,
    SourceMirrors,
    SqliteDeckStore,
    SqliteSourceRunStore,
    SqliteSourceStore,
)
from presentator.adapters.identity import (
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    TokenIdentifierFactory,
)
from presentator.adapters.key_drafts import SqliteSourceKeyDrafts
from presentator.adapters.preferences import SqlitePersonPreferencesStore
from presentator.adapters.schema import create_tables
from presentator.adapters.secrets import secret_box
from presentator.application.decks import Decks
from presentator.application.people import People
from presentator.contracts.decks import (
    Build,
    Deck,
    Source,
    SourceRun,
    SourceRunOutcome,
    SourceWrite,
)
from presentator.contracts.models import Role, User
from presentator.contracts.preferences import PersonPreferences
from tests.adapters.key_draft_support import INSTANCE_KEY, a_key_drafts_store
from tests.adapters.test_decks import OpenCredentials
from tests.application.fakes import (
    FakeConnectionChecker,
    FakeToolchainThemes,
    FrozenClock,
)
from tests.people import seed_user

_ACCOUNTS = "users"
_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
THE_PERSON = User(id="the-person", username="pia", role=Role.ADMIN)
ANOTHER = User(id="another-person", username="ben", role=Role.USER)
_A_BOUND = timedelta(seconds=30)
WHAT_THE_GIT_HOST_EXPECTS = "the read-only words only this test made up"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"


@dataclass
class Instance:
    """A database and the directories beside it, holding two people's rows."""

    database: Path
    mirrors: SourceMirrors
    builds: Path
    people: People
    kept_by: dict[str, tuple[Source, Path, Path]]

    def mirror_of(self, owner: User) -> Path:
        return self.kept_by[owner.id][1]

    def build_of(self, owner: User) -> Path:
        return self.kept_by[owner.id][2]


@dataclass(frozen=True)
class _Stores:
    database: Path
    sources: SqliteSourceStore
    decks: SqliteDeckStore
    runs: SqliteSourceRunStore
    key_drafts: SqliteSourceKeyDrafts
    accounts: SqliteSubjectUserStore
    sessions: SqliteSessionRecordStore
    preferences: SqlitePersonPreferencesStore


def _stores_over(database: Path) -> _Stores:
    return _Stores(
        database=database,
        sources=SqliteSourceStore(
            database=database,
            identifiers=TokenIdentifierFactory(),
            box=secret_box(INSTANCE_KEY),
        ),
        decks=SqliteDeckStore(database=database),
        runs=SqliteSourceRunStore(database=database),
        key_drafts=a_key_drafts_store(database),
        accounts=SqliteSubjectUserStore(database, ProcessWriteLock()),
        sessions=SqliteSessionRecordStore(database),
        preferences=SqlitePersonPreferencesStore(database),
    )


def _seed_person(
    person: User,
    stores: _Stores,
    mirrors: SourceMirrors,
    builds: Path,
) -> tuple[Source, Path, Path]:
    seed_user(stores.database, person)
    stores.sessions.create(
        person.id,
        now=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        refresh_token=SecretStr("a-refresh-token"),
    )
    stores.preferences.write(person.id, PersonPreferences(language_tag="de"))
    stores.key_drafts.mint(person.id, at=_NOW)
    source = stores.sources.add(
        SourceWrite(
            name=f"{person.username}-decks",
            url=f"git@example.invalid:{person.username}.git",
            ref="main",
            owner_id=person.id,
            access_secret=WHAT_THE_GIT_HOST_EXPECTS,
            hook_secret_hash=b"\x11" * 32,
            public_key=None,
        ),
    )
    assert source is not None
    mirror_directory = mirrors.of(source)
    assert mirror_directory is not None
    mirror = mirror_directory.directory
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/main")
    build = builds / f"{person.username}-run" / _COMMIT / "talk"
    build.mkdir(parents=True)
    (build / "index.html").write_text("a talk")
    _seed_deck(stores, person, source, build)
    return source, mirror, build


def _seed_deck(stores: _Stores, person: User, source: Source, build: Path) -> None:
    slug = f"{person.username}-talk"
    assert stores.decks.put(
        Deck(
            slug=slug,
            title="A talk",
            changed_at=_NOW,
            owner_id=person.id,
            source_id=source.id,
            commit=_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    stores.decks.put_build(
        slug,
        Build(
            directory=build,
            pdf=build.parent / "deck.pdf",
            commit=_COMMIT,
            built_at=_NOW,
        ),
    )
    stores.runs.record(
        SourceRun(
            source_id=source.id,
            at=_NOW,
            outcome=SourceRunOutcome.SUCCESS,
            commit=_COMMIT,
            reason=None,
        ),
    )


def _decks_application(
    stores: _Stores,
    mirrors: SourceMirrors,
    builds: Path,
    tmp_path: Path,
) -> Decks:
    return Decks(
        sources=stores.sources,
        key_drafts=stores.key_drafts,
        folders=MirroredDeckFolders(mirrors=mirrors),
        store=stores.decks,
        builder=SlidevBuilds(
            builds=builds,
            toolchain=HostToolchain(project=tmp_path, bound=_A_BOUND),
            mirrors=mirrors,
            output_megabytes=1,
        ),
        source_runs=stores.runs,
        checker=FakeConnectionChecker(),
        toolchain_themes=FakeToolchainThemes(),
        clock=FrozenClock(instant=_NOW),
        local_mount=FilesystemLocalMount(mount=tmp_path),
        build_bound=_A_BOUND,
        fingerprint_key=b"a fingerprint key of some length",
    )


def an_instance_with_both_people(tmp_path: Path) -> Instance:
    """Each person has an account, a session, preferences, a draft and a source.

    The source carries a deck with a build, a run, a mirror and a build
    directory on disk.
    """
    database = tmp_path / "presentator.sqlite3"
    create_tables(database)
    mirrors = SourceMirrors(
        directory=tmp_path / "mirrors",
        credentials=OpenCredentials(),
        pull_timeout=_A_BOUND,
        local_mount=FilesystemLocalMount(mount=tmp_path),
    )
    builds = tmp_path / "builds"
    stores = _stores_over(database)
    kept_by = {
        person.id: _seed_person(person, stores, mirrors, builds)
        for person in (THE_PERSON, ANOTHER)
    }
    return Instance(
        database=database,
        mirrors=mirrors,
        builds=builds,
        people=People(
            decks=_decks_application(stores, mirrors, builds, tmp_path),
            accounts=stores.accounts,
            sessions=stores.sessions,
            preferences=stores.preferences,
            key_drafts=stores.key_drafts,
        ),
        kept_by=kept_by,
    )


# Finding, by the schema's own foreign keys, every row a person owns.


def tables(connection: sqlite3.Connection) -> list[str]:
    return [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        )
    ]


def tables_reaching_accounts(connection: sqlite3.Connection) -> set[str]:
    """Every table that references `users`, or names a `user_id` or `owner_id`."""
    references = {
        table: {
            row[2] for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        for table in tables(connection)
    }
    reached = {_ACCOUNTS} | {
        table
        for table in references
        if {"user_id", "owner_id"}
        & {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    }
    grew = True
    while grew:
        grew = False
        for table, targets in references.items():
            if table not in reached and targets & reached:
                reached.add(table)
                grew = True
    return reached


def owned_row_ids(
    connection: sqlite3.Connection, person_id: str
) -> dict[str, set[int]]:
    """Per table that reaches `users`, the rowids the person owns, parents first.

    Taken before an erasure, these name rows that stay findable after their
    parents are gone, so an orphan is still counted.
    """
    reaching = tables_reaching_accounts(connection)
    owned: dict[str, set[int]] = {table: set() for table in reaching}
    owned[_ACCOUNTS] = {
        row[0]
        for row in connection.execute(
            "SELECT rowid FROM users WHERE id = ?",
            (person_id,),
        )
    }
    grew = True
    while grew:
        grew = False
        for table in reaching - {_ACCOUNTS}:
            for foreign_key in _foreign_keys(connection, table):
                parent = foreign_key.parent
                if parent not in reaching:
                    continue
                found = _rows_following(connection, table, foreign_key, owned[parent])
                if not found <= owned[table]:
                    owned[table] |= found
                    grew = True
    return owned


def rows_still_there(
    connection: sqlite3.Connection, owned: dict[str, set[int]]
) -> dict[str, int]:
    """Per table, how many of the previously owned rowids still exist."""
    return {
        table: connection.execute(
            _COUNT_OF_ROWIDS.format(table=table), (json.dumps(sorted(ids)),)
        ).fetchone()[0]
        for table, ids in owned.items()
    }


class ForeignKey(NamedTuple):
    """The columns of `PRAGMA foreign_key_list` the guard reads.

    The pragma answers eight columns; `target` is NULL when the reference names
    the parent's primary key.
    """

    parent: str
    source: str
    target: str | None


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[ForeignKey]:
    return [
        ForeignKey(parent=row[2], source=row[3], target=row[4])
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    ]


_COUNT_OF_ROWIDS = (
    'SELECT COUNT(*) FROM "{table}" WHERE rowid IN (SELECT value FROM json_each(?))'
)
_ROWS_FOLLOWING = (
    'SELECT rowid FROM "{table}" WHERE "{child}" IN'
    ' (SELECT "{key}" FROM "{parent}" WHERE rowid IN (SELECT value FROM json_each(?)))'
)


def _rows_following(
    connection: sqlite3.Connection,
    table: str,
    foreign_key: ForeignKey,
    parent_rowids: set[int],
) -> set[int]:
    if foreign_key.target is None:
        message = (
            f"{table}.{foreign_key.source} references {foreign_key.parent} without"
            " naming a column, which the guard cannot follow"
        )
        raise ValueError(message)
    query = _ROWS_FOLLOWING.format(
        table=table,
        child=foreign_key.source,
        key=foreign_key.target,
        parent=foreign_key.parent,
    )
    return {
        row[0]
        for row in connection.execute(query, (json.dumps(sorted(parent_rowids)),))
    }


def owned_by(database: Path, person: User) -> dict[str, set[int]]:
    """Per table the guard finds, the rows the person owns, parents first."""
    with closing(sqlite3.connect(database)) as connection:
        return owned_row_ids(connection, person.id)


def still_there(database: Path, owned: dict[str, set[int]]) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        return rows_still_there(connection, owned)
