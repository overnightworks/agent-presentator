"""Deck folders read out of a real repository, and deck rows in a real file."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitmirror.model import CredentialReference
from presentator.adapters.decks import (
    ConfiguredSource,
    EnvironmentCredentials,
    MirroredDeckFolders,
    SourceMirrors,
    SqliteDeckStore,
    SqliteSourceStore,
    create_deck_tables,
)
from presentator.adapters.identity import (
    SqliteUserStore,
    TokenIdentifierFactory,
    create_identity_tables,
)
from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    Build,
    Deck,
    DeckFolder,
    Source,
)
from presentator.contracts.models import Credentials, Role, User
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_NOTICED_GONE_AT = datetime(2026, 1, 20, 9, tzinfo=UTC)
_OWNER = User(id="the-admin", username="felix", role=Role.ADMIN)
_SOURCE_ID = "the-source-that-carried-it"
_ANOTHER_SOURCE_ID = "a-second-source"
_SOURCE_NAME = "decks"
_ANOTHER_SOURCE_NAME = "talks"
_ADDRESS = "git@example.invalid:decks.git"
_ANOTHER_ADDRESS = "git@example.invalid:talks.git"
_CREDENTIAL_VARIABLE = "A_READ_ONLY_TOKEN"
_A_GENEROUS_BOUND = timedelta(seconds=30)
_NO_BUDGET_AT_ALL = timedelta(0)
_A_STORED_HASH = "the hash first start stored"
_BUILT_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_A_LATER_COMMIT = "b7c1d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"


def a_source(url: str, *, credential: str | None = None) -> Source:
    return Source(
        id=_SOURCE_ID,
        name=_SOURCE_NAME,
        url=url,
        ref=MAIN_BRANCH,
        credential_reference=credential,
        owner_id=_OWNER.id,
    )


def folders_under(
    tmp_path: Path,
    *,
    pull_timeout: timedelta = _A_GENEROUS_BOUND,
) -> MirroredDeckFolders:
    return MirroredDeckFolders(mirrors=mirrors_under(tmp_path, timeout=pull_timeout))


def mirrors_under(
    tmp_path: Path,
    *,
    timeout: timedelta = _A_GENEROUS_BOUND,
) -> SourceMirrors:
    return SourceMirrors(
        directory=tmp_path / "mirrors",
        credentials=EnvironmentCredentials(),
        pull_timeout=timeout,
    )


def folders_read(tmp_path: Path, source: Source) -> tuple[DeckFolder, ...]:
    """What a source a test has made readable carries."""
    read = folders_under(tmp_path).folders(source)
    assert read is not None
    return read


def a_deck(
    slug: str = "kundenfeedback",
    *,
    title: str = "Kundenfeedback",
    commit: str = _COMMIT,
    source_id: str = _SOURCE_ID,
) -> Deck:
    return Deck(
        slug=slug,
        title=title,
        changed_at=_PUSHED_AT,
        owner_id=_OWNER.id,
        source_id=source_id,
        commit=commit,
        build=None,
    )


def a_build(tmp_path: Path, *, commit: str = _COMMIT) -> Build:
    return Build(
        directory=tmp_path / "builds" / "kundenfeedback" / commit / "talk",
        pdf=tmp_path / "builds" / "kundenfeedback" / commit / "deck.pdf",
        commit=commit,
        built_at=_BUILT_AT,
    )


def a_deck_store(tmp_path: Path) -> SqliteDeckStore:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    return SqliteDeckStore(database=database)


def a_source_store(
    database: Path,
    *,
    url: str | None = _ADDRESS,
    name: str = _SOURCE_NAME,
) -> SqliteSourceStore:
    """The sources table of an instance configured with that address."""
    return SqliteSourceStore(
        database=database,
        configured=ConfiguredSource(
            name=name,
            url=url,
            ref=MAIN_BRANCH,
            credential_reference=None,
            accounts=SqliteUserStore(database),
        ),
        identifiers=TokenIdentifierFactory(),
    )


# The deck table as this product wrote it before a source was a row of its own,
# so what an upgrade finds is a file and not a description of one.
_DECKS_BEFORE_SOURCES = """
CREATE TABLE decks (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id),
    commit_sha TEXT NOT NULL,
    active_build TEXT,
    pdf_export TEXT,
    built_commit TEXT,
    built_at TEXT,
    removed_at TEXT
);
"""
_A_DECK_BEFORE_SOURCES = """
INSERT INTO decks (slug, title, changed_at, owner_id, commit_sha)
VALUES (?, ?, ?, ?, ?)
"""


def a_database_written_before_sources(tmp_path: Path) -> Path:
    """A file this product wrote before sources were rows, carrying one deck."""
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    SqliteUserStore(database).add_first_account(
        Credentials(user=_OWNER, password_hash=_A_STORED_HASH),
    )
    apply_schema(database, _DECKS_BEFORE_SOURCES)
    kept = a_deck()
    with rows(database) as cursor:
        cursor.execute(
            _A_DECK_BEFORE_SOURCES,
            (
                kept.slug,
                kept.title,
                kept.changed_at.isoformat(),
                kept.owner_id,
                kept.commit,
            ),
        )
    return database


def an_instance_that_was_set_up(tmp_path: Path) -> Path:
    """A database with every table and the admin first start created."""
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    SqliteUserStore(database).add_first_account(
        Credentials(user=_OWNER, password_hash=_A_STORED_HASH),
    )
    return database


def test_a_pushed_deck_folder_is_read_with_its_title_and_its_change_time(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    folders = folders_read(tmp_path, a_source(remote.url))

    assert len(folders) == 1
    read = folders[0]
    assert read.name == EXAMPLE_SLUG
    assert read.title == EXAMPLE_TITLE
    assert {MANIFEST_FILE, SLIDES_FILE} <= read.file_names
    assert read.changed_at == _PUSHED_AT
    assert read.commit == remote.head


def test_a_push_that_touched_another_folder_leaves_this_decks_commit_alone(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)
    pushed_first = remote.head

    remote.commit({"another-deck/slides.md": "# Elsewhere\n"}, at=_PUSHED_AT)
    read = {
        folder.name: folder for folder in folders_read(tmp_path, a_source(remote.url))
    }

    assert read[EXAMPLE_SLUG].commit == pushed_first
    assert read["another-deck"].commit == remote.head


def test_a_folder_without_a_manifest_is_read_without_a_title(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({"just-notes/slides.md": "# notes\n"}, at=_PUSHED_AT)

    folders = folders_read(tmp_path, a_source(remote.url))

    assert [folder.title for folder in folders] == [None]


@pytest.mark.parametrize(
    ("broken", "manifest"),
    [
        ("nameless", 'language = "en"\n'),
        ("not-toml", "this is not a manifest at all\n"),
    ],
    ids=["names no title", "is not TOML"],
)
def test_a_folder_whose_manifest_is_unreadable_is_still_there_without_a_title(
    broken: str,
    manifest: str,
    caplog: pytest.LogCaptureFixture,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)
    remote.commit(
        {f"{broken}/deck.toml": manifest, f"{broken}/slides.md": "# x\n"},
        at=_PUSHED_AT,
    )

    with caplog.at_level(logging.WARNING):
        folders = folders_read(tmp_path, a_source(remote.url))

    assert {folder.name: folder.title for folder in folders} == {
        EXAMPLE_SLUG: EXAMPLE_TITLE,
        broken: None,
    }
    assert broken in caplog.text


def test_a_pull_that_runs_past_its_bound_says_nothing_rather_than_waiting(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    folders = folders_under(tmp_path, pull_timeout=_NO_BUDGET_AT_ALL).folders(
        a_source(remote.url),
    )

    assert folders is None


def test_a_source_that_cannot_be_read_says_nothing_about_its_folders(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    unreachable = a_source((tmp_path / "nothing.git").as_uri())

    with caplog.at_level(logging.WARNING):
        folders = folders_under(tmp_path).folders(unreachable)

    assert folders is None
    assert "unreachable" in caplog.text


def test_a_credential_reference_is_resolved_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_CREDENTIAL_VARIABLE, "what only this test wrote")
    reference = CredentialReference(name=_CREDENTIAL_VARIABLE)

    resolved = EnvironmentCredentials().resolve(reference)
    monkeypatch.delenv(_CREDENTIAL_VARIABLE)

    assert resolved == "what only this test wrote"
    assert EnvironmentCredentials().resolve(reference) is None


def test_a_credential_reference_the_environment_does_not_carry_stops_the_read(
    monkeypatch: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(_CREDENTIAL_VARIABLE, raising=False)
    remote.commit_example_deck(at=_PUSHED_AT)

    folders = folders_under(tmp_path).folders(
        a_source(remote.url, credential=_CREDENTIAL_VARIABLE),
    )

    assert folders is None


def test_pushing_the_same_folder_again_leaves_one_deck_under_its_slug(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))

    store.put(a_deck(title="Kundenfeedback Q3", commit=_A_LATER_COMMIT))

    kept = a_deck(title="Kundenfeedback Q3", commit=_A_LATER_COMMIT)
    assert store.all() == (kept,)
    assert store.get(kept.slug) == kept
    assert store.get("never-pushed") is None


def test_a_deck_whose_folder_vanished_is_gone_from_the_list_until_it_returns(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))
    store.put(a_deck("knowledge-fabric"))

    store.mark_removed_except(
        present=frozenset({"knowledge-fabric"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )
    while_it_was_gone = store.all()
    store.put(a_deck("kundenfeedback", title="Kundenfeedback Q4"))
    store.mark_removed_except(
        present=frozenset({"knowledge-fabric", "kundenfeedback"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert [deck.slug for deck in while_it_was_gone] == ["knowledge-fabric"]
    assert sorted(store.all(), key=lambda deck: deck.slug) == [
        a_deck("knowledge-fabric"),
        a_deck("kundenfeedback", title="Kundenfeedback Q4"),
    ]


def test_a_removed_decks_own_page_is_unreachable_by_slug(tmp_path: Path) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))

    store.mark_removed_except(
        present=frozenset(),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert store.get("kundenfeedback") is None


def test_a_slug_carrying_an_apostrophe_and_a_non_ascii_letter_survives_reconciliation(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    kept_slug = "l'équipe"
    store.put(a_deck(kept_slug))
    store.put(a_deck("gone"))

    store.mark_removed_except(
        present=frozenset({kept_slug}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert store.get(kept_slug) == a_deck(kept_slug)
    assert store.get("gone") is None


def test_everything_a_build_wrote_switches_over_together(tmp_path: Path) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    first = a_build(tmp_path)

    store.put_build("kundenfeedback", first)
    after_the_first = store.get("kundenfeedback")
    store.put_build("kundenfeedback", a_build(tmp_path, commit=_A_LATER_COMMIT))
    after_the_second = store.get("kundenfeedback")

    assert after_the_first is not None
    assert after_the_first.build == first
    assert after_the_second is not None
    assert after_the_second.build == a_build(tmp_path, commit=_A_LATER_COMMIT)


def test_taking_a_deck_in_again_leaves_what_it_delivers_standing(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    built = a_build(tmp_path)
    store.put_build("kundenfeedback", built)

    store.put(a_deck(title="Kundenfeedback Q3"))
    kept = store.get("kundenfeedback")

    assert kept is not None
    assert kept.build == built


def test_the_configured_source_becomes_one_row_owned_by_the_first_admin(
    tmp_path: Path,
) -> None:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    sources = a_source_store(database)

    sources.seed()
    before_first_start = sources.all()
    SqliteUserStore(database).add_first_account(
        Credentials(user=_OWNER, password_hash=_A_STORED_HASH),
    )
    sources.seed()
    sources.seed()

    assert before_first_start == ()
    seeded = sources.all()
    assert [(source.name, source.url, source.ref) for source in seeded] == [
        (_SOURCE_NAME, _ADDRESS, MAIN_BRANCH),
    ]
    assert seeded[0].owner_id == _OWNER.id
    assert seeded[0].credential_reference is None


def test_a_second_configured_address_is_a_second_source(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    a_source_store(database).seed()

    a_source_store(
        database,
        url=_ANOTHER_ADDRESS,
        name=_ANOTHER_SOURCE_NAME,
    ).seed()

    assert {source.url for source in a_source_store(database).all()} == {
        _ADDRESS,
        _ANOTHER_ADDRESS,
    }


def test_an_address_under_a_name_another_source_answers_to_is_not_stored(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    a_source_store(database).seed()

    with caplog.at_level(logging.WARNING):
        a_source_store(database, url=_ANOTHER_ADDRESS).seed()

    assert [source.url for source in a_source_store(database).all()] == [_ADDRESS]
    assert _SOURCE_NAME in caplog.text
    assert _ANOTHER_ADDRESS not in caplog.text


def test_an_instance_without_a_configured_url_stores_no_source(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)

    a_source_store(database, url=None).seed()

    assert a_source_store(database).all() == ()


def test_a_database_written_before_sources_keeps_its_decks_and_names_the_seeded_one(
    tmp_path: Path,
) -> None:
    database = a_database_written_before_sources(tmp_path)

    create_deck_tables(database)
    sources = a_source_store(database)
    sources.seed()

    seeded = sources.all()
    assert [source.url for source in seeded] == [_ADDRESS]
    assert SqliteDeckStore(database=database).all() == (a_deck(source_id=seeded[0].id),)


def test_a_folder_name_another_source_carries_is_not_taken_over(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="The first"))

    taken_over = store.put(
        a_deck(title="The second", source_id=_ANOTHER_SOURCE_ID),
    )

    assert taken_over is False
    assert store.all() == (a_deck(title="The first"),)


def test_a_removed_slug_stays_with_its_source_and_is_the_deck_it_was(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="The first"))
    store.mark_removed_except(
        present=frozenset(),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    taken_over = store.put(a_deck(title="The second", source_id=_ANOTHER_SOURCE_ID))
    while_it_was_gone = store.all()

    store.put(a_deck(title="The first"))
    store.mark_removed_except(
        present=frozenset({"kundenfeedback"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert taken_over is False
    assert while_it_was_gone == ()
    assert store.all() == (a_deck(title="The first"),)


def test_one_sources_reconciliation_leaves_another_sources_decks_alone(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))
    store.put(a_deck("knowledge-fabric", source_id=_ANOTHER_SOURCE_ID))

    store.mark_removed_except(
        present=frozenset(),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert store.all() == (a_deck("knowledge-fabric", source_id=_ANOTHER_SOURCE_ID),)
