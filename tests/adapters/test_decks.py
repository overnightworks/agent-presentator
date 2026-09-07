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
    SqliteDeckStore,
    create_deck_tables,
)
from presentator.adapters.identity import SqliteUserStore, create_identity_tables
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    Deck,
    DeckFolder,
    Source,
)
from presentator.contracts.models import Credentials, Role, User
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_NOTICED_GONE_AT = datetime(2026, 1, 20, 9, tzinfo=UTC)
_OWNER = User(id="the-admin", username="felix", role=Role.ADMIN)
_CREDENTIAL_VARIABLE = "A_READ_ONLY_TOKEN"
_A_GENEROUS_BOUND = timedelta(seconds=30)
_NO_BUDGET_AT_ALL = timedelta(0)
_A_STORED_HASH = "the hash first start stored"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_A_LATER_COMMIT = "b7c1d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"


def a_source(url: str, *, credential: str | None = None) -> Source:
    return Source(
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
    return MirroredDeckFolders(
        mirrors=tmp_path / "mirrors",
        credentials=EnvironmentCredentials(),
        pull_timeout=pull_timeout,
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
) -> Deck:
    return Deck(
        slug=slug,
        title=title,
        changed_at=_PUSHED_AT,
        owner_id=_OWNER.id,
        commit=commit,
        active_build=None,
        pdf_export=None,
    )


def a_deck_store(tmp_path: Path) -> SqliteDeckStore:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    return SqliteDeckStore(database=database)


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
        at=_NOTICED_GONE_AT,
    )
    while_it_was_gone = store.all()
    store.put(a_deck("kundenfeedback", title="Kundenfeedback Q4"))
    store.mark_removed_except(
        present=frozenset({"knowledge-fabric", "kundenfeedback"}),
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

    store.mark_removed_except(present=frozenset(), at=_NOTICED_GONE_AT)

    assert store.get("kundenfeedback") is None


def test_a_slug_carrying_an_apostrophe_and_a_non_ascii_letter_survives_reconciliation(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    kept_slug = "l'équipe"
    store.put(a_deck(kept_slug))
    store.put(a_deck("gone"))

    store.mark_removed_except(present=frozenset({kept_slug}), at=_NOTICED_GONE_AT)

    assert store.get(kept_slug) == a_deck(kept_slug)
    assert store.get("gone") is None


def test_the_talk_a_deck_delivers_is_the_directory_that_was_put_last(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    built = tmp_path / "builds" / "kundenfeedback"

    store.put_active_build("kundenfeedback", directory=built)
    kept = store.get("kundenfeedback")

    assert kept is not None
    assert kept.active_build == built


def test_the_pdf_a_deck_hands_over_is_the_file_that_was_put_last(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    exported = tmp_path / "exports" / "kundenfeedback.pdf"

    store.put_pdf_export("kundenfeedback", file=exported)
    kept = store.get("kundenfeedback")

    assert kept is not None
    assert kept.pdf_export == exported


def test_taking_a_deck_in_again_leaves_what_it_delivers_standing(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    built = tmp_path / "builds" / "kundenfeedback"
    exported = tmp_path / "exports" / "kundenfeedback.pdf"
    store.put_active_build("kundenfeedback", directory=built)
    store.put_pdf_export("kundenfeedback", file=exported)

    store.put(a_deck(title="Kundenfeedback Q3"))
    kept = store.get("kundenfeedback")

    assert kept is not None
    assert kept.active_build == built
    assert kept.pdf_export == exported


def test_the_configured_source_belongs_to_the_account_that_set_the_instance_up(
    tmp_path: Path,
) -> None:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    accounts = SqliteUserStore(database)
    configured = ConfiguredSource(
        url="git@example.invalid:decks.git",
        ref=MAIN_BRANCH,
        credential_reference=None,
        accounts=accounts,
    )

    before_first_start = configured.configured()
    accounts.add_first_account(Credentials(user=_OWNER, password_hash=_A_STORED_HASH))

    assert before_first_start is None
    assert configured.configured() == a_source("git@example.invalid:decks.git")


def test_an_instance_without_a_configured_url_has_no_source(tmp_path: Path) -> None:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    accounts = SqliteUserStore(database)
    accounts.add_first_account(Credentials(user=_OWNER, password_hash=_A_STORED_HASH))

    unconfigured = ConfiguredSource(
        url=None,
        ref=MAIN_BRANCH,
        credential_reference=None,
        accounts=accounts,
    )

    assert unconfigured.configured() is None
