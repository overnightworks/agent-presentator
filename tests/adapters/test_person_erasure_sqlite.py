"""Erasing a person over real SQLite and real mirror and build directories."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from presentator.contracts.people import PersonErased
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
_ANOTHER = User(id="another-person", username="ben", role=Role.USER)
_A_BOUND = timedelta(seconds=30)
_WHAT_THE_GIT_HOST_EXPECTS = "the read-only words only this test made up"
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
    sources = SqliteSourceStore(
        database=database,
        identifiers=TokenIdentifierFactory(),
        box=secret_box(INSTANCE_KEY),
    )
    decks = SqliteDeckStore(database=database)
    runs = SqliteSourceRunStore(database=database)
    key_drafts = a_key_drafts_store(database)
    accounts = SqliteSubjectUserStore(database, ProcessWriteLock())
    sessions = SqliteSessionRecordStore(database)
    preferences = SqlitePersonPreferencesStore(database)
    kept_by: dict[str, tuple[Source, Path, Path]] = {}
    for person in (THE_PERSON, _ANOTHER):
        seed_user(database, person)
        sessions.create(
            person.id,
            now=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            refresh_token=SecretStr("a-refresh-token"),
        )
        preferences.write(person.id, PersonPreferences(language_tag="de"))
        key_drafts.mint(person.id, at=_NOW)
        source = sources.add(
            SourceWrite(
                name=f"{person.username}-decks",
                url=f"git@example.invalid:{person.username}.git",
                ref="main",
                owner_id=person.id,
                access_secret=_WHAT_THE_GIT_HOST_EXPECTS,
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
        slug = f"{person.username}-talk"
        assert decks.put(
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
        decks.put_build(
            slug,
            Build(
                directory=build,
                pdf=build.parent / "deck.pdf",
                commit=_COMMIT,
                built_at=_NOW,
            ),
        )
        runs.record(
            SourceRun(
                source_id=source.id,
                at=_NOW,
                outcome=SourceRunOutcome.SUCCESS,
                commit=_COMMIT,
                reason=None,
            ),
        )
        kept_by[person.id] = (source, mirror, build)
    application = Decks(
        sources=sources,
        key_drafts=key_drafts,
        folders=MirroredDeckFolders(mirrors=mirrors),
        store=decks,
        builder=SlidevBuilds(
            builds=builds,
            toolchain=HostToolchain(project=tmp_path, bound=_A_BOUND),
            mirrors=mirrors,
            output_megabytes=1,
        ),
        source_runs=runs,
        checker=FakeConnectionChecker(),
        toolchain_themes=FakeToolchainThemes(),
        clock=FrozenClock(instant=_NOW),
        local_mount=FilesystemLocalMount(mount=tmp_path),
        build_bound=_A_BOUND,
        fingerprint_key=b"a fingerprint key of some length",
    )
    return Instance(
        database=database,
        mirrors=mirrors,
        builds=builds,
        people=People(
            decks=application,
            accounts=accounts,
            sessions=sessions,
            preferences=preferences,
            key_drafts=key_drafts,
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
    """Every table that references `users`, directly or through another table."""
    references = {
        table: {
            row[2] for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        for table in tables(connection)
    }
    reached = {_ACCOUNTS}
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
            for foreign_key in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall():
                parent = foreign_key[2]
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
    foreign_key: tuple[int, int, str, str, str],
    parent_rowids: set[int],
) -> set[int]:
    query = _ROWS_FOLLOWING.format(
        table=table, child=foreign_key[3], key=foreign_key[4], parent=foreign_key[2]
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


def test_a_person_with_every_kind_of_row_leaves_none_and_no_mirror_or_build(
    tmp_path: Path,
) -> None:
    """S9 (#293) Done when: no table holds a row of the person, nor mirror or build."""
    instance = an_instance_with_both_people(tmp_path)
    owned = owned_by(instance.database, THE_PERSON)
    assert all(owned.values())

    outcome = instance.people.erase_person(THE_PERSON.id, dry_run=False)

    assert isinstance(outcome, PersonErased)
    assert not any(still_there(instance.database, owned).values())
    assert not any(owned_by(instance.database, THE_PERSON).values())
    assert not instance.mirror_of(THE_PERSON).exists()
    assert not instance.build_of(THE_PERSON).exists()


def test_a_second_person_keeps_every_row_mirror_and_build(tmp_path: Path) -> None:
    """E5 (#418): the erasure takes only the named person's rows."""
    instance = an_instance_with_both_people(tmp_path)
    before = owned_by(instance.database, _ANOTHER)

    instance.people.erase_person(THE_PERSON.id, dry_run=False)

    assert owned_by(instance.database, _ANOTHER) == before
    assert instance.mirror_of(_ANOTHER).exists()
    assert instance.build_of(_ANOTHER).exists()


def test_a_dry_run_changes_no_row_and_no_directory(tmp_path: Path) -> None:
    """S9 (#293): the dry run changes nothing."""
    instance = an_instance_with_both_people(tmp_path)
    before = owned_by(instance.database, THE_PERSON)

    instance.people.erase_person(THE_PERSON.id, dry_run=True)

    assert owned_by(instance.database, THE_PERSON) == before
    assert instance.mirror_of(THE_PERSON).exists()
    assert instance.build_of(THE_PERSON).exists()


def test_an_unknown_subject_creates_no_account(tmp_path: Path) -> None:
    """S9 (#293): an unknown subject creates no `users` row."""
    instance = an_instance_with_both_people(tmp_path)

    instance.people.erase_person("nobody-at-all", dry_run=False)
    instance.people.erase_person("nobody-at-all", dry_run=True)

    with closing(sqlite3.connect(instance.database)) as connection:
        accounts = connection.execute("SELECT id FROM users ORDER BY id").fetchall()
    assert accounts == [(_ANOTHER.id,), (THE_PERSON.id,)]


def test_the_sole_admin_is_erased_like_anyone_and_a_repeat_is_at_zero(
    tmp_path: Path,
) -> None:
    """people.md: the sole admin goes like anyone; a repeat is idempotent."""
    instance = an_instance_with_both_people(tmp_path)

    first = instance.people.erase_person(THE_PERSON.id, dry_run=False)
    again = instance.people.erase_person(THE_PERSON.id, dry_run=False)

    assert isinstance(first, PersonErased)
    assert isinstance(again, PersonErased)
    assert all(entry.count == 0 for entry in again.kinds)
