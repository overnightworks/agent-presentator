"""Deck folders read out of a real repository, and deck rows in a real file."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from presentator.adapters.decks import (
    ConfiguredSource,
    EnvironmentCredentials,
    MalformedManifestError,
    MirroredDeckFolders,
    SqliteDeckStore,
    create_deck_tables,
)
from presentator.adapters.identity import SqliteUserStore, create_identity_tables
from presentator.contracts.decks import MANIFEST_FILE, SLIDES_FILE, Deck, Source
from presentator.contracts.models import Credentials, Role, User
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_OWNER = User(id="the-admin", username="felix", role=Role.ADMIN)
_CREDENTIAL_VARIABLE = "A_READ_ONLY_TOKEN"
_A_STORED_HASH = "the hash first start stored"


def a_source(url: str, *, credential: str | None = None) -> Source:
    return Source(
        url=url,
        ref=MAIN_BRANCH,
        credential_reference=credential,
        owner_id=_OWNER.id,
    )


def folders_under(tmp_path: Path) -> MirroredDeckFolders:
    return MirroredDeckFolders(
        mirrors=tmp_path / "mirrors",
        credentials=EnvironmentCredentials(),
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

    folders = folders_under(tmp_path).folders(a_source(remote.url))

    assert len(folders) == 1
    read = folders[0]
    assert read.name == EXAMPLE_SLUG
    assert read.title == EXAMPLE_TITLE
    assert {MANIFEST_FILE, SLIDES_FILE} <= read.file_names
    assert read.changed_at == _PUSHED_AT


def test_a_folder_without_a_manifest_is_read_without_a_title(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({"just-notes/slides.md": "# notes\n"}, at=_PUSHED_AT)

    folders = folders_under(tmp_path).folders(a_source(remote.url))

    assert [folder.title for folder in folders] == [None]


def test_a_manifest_that_names_no_title_is_refused(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(
        {"nameless/deck.toml": 'language = "en"\n', "nameless/slides.md": "# x\n"},
        at=_PUSHED_AT,
    )

    with pytest.raises(MalformedManifestError, match="nameless"):
        folders_under(tmp_path).folders(a_source(remote.url))


def test_a_source_that_cannot_be_read_yields_no_folders_and_says_so(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    unreachable = a_source((tmp_path / "nothing.git").as_uri())

    with caplog.at_level(logging.WARNING):
        folders = folders_under(tmp_path).folders(unreachable)

    assert folders == ()
    assert "unreachable" in caplog.text


def test_a_credential_reference_is_resolved_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(_CREDENTIAL_VARIABLE, "a read-only token")
    remote.commit_example_deck(at=_PUSHED_AT)

    folders = folders_under(tmp_path).folders(
        a_source(remote.url, credential=_CREDENTIAL_VARIABLE),
    )

    assert [folder.name for folder in folders] == [EXAMPLE_SLUG]


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

    assert folders == ()


def test_pushing_the_same_folder_again_leaves_one_deck_under_its_slug(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(
        Deck(
            slug="kundenfeedback",
            title="Kundenfeedback",
            changed_at=_PUSHED_AT,
            owner_id=_OWNER.id,
        ),
    )

    store.put(
        Deck(
            slug="kundenfeedback",
            title="Kundenfeedback Q3",
            changed_at=_PUSHED_AT,
            owner_id=_OWNER.id,
        ),
    )

    assert store.all() == (
        Deck(
            slug="kundenfeedback",
            title="Kundenfeedback Q3",
            changed_at=_PUSHED_AT,
            owner_id=_OWNER.id,
        ),
    )


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
