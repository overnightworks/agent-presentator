"""The deck list as a browser receives it."""

from datetime import UTC, datetime, timedelta
from functools import partial
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from presentator.adapters.catalog import ENGLISH_CATALOG, age_in_words, load_lobby_text
from presentator.api.auth import Wording, create_lobby
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.contracts.decks import MANIFEST_FILE, SLIDES_FILE, DeckFolder, Source
from presentator.contracts.text import LobbyText
from tests.application.fakes import (
    CountingIdentifierFactory,
    FakeDeckFolders,
    FakeDeckStore,
    FakeLoginAttemptStore,
    FakeSessionRecordStore,
    FakeSourceStore,
    FakeUserStore,
    FrozenClock,
    MarkingCookieSigner,
    ReversibleHasher,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_PERSON = "felix"
_TYPED_WORDS = "the words only this test types"
_ADDRESS = "git@heimserver:decks.git"
_TEXT: LobbyText = load_lobby_text(ENGLISH_CATALOG)
_A_DECK = frozenset({MANIFEST_FILE, SLIDES_FILE})


def a_folder(name: str, *, title: str, changed_ago: timedelta) -> DeckFolder:
    return DeckFolder(
        name=name,
        file_names=_A_DECK,
        title=title,
        changed_at=_NOW - changed_ago,
    )


def a_signed_in_lobby(
    *folders: DeckFolder,
    source: Source | None = None,
) -> TestClient:
    clock = FrozenClock(instant=_NOW)
    decks = Decks(
        sources=FakeSourceStore(source=source),
        folders=FakeDeckFolders(found=folders),
        store=FakeDeckStore(),
        clock=clock,
    )
    # The list shows what a refresh stored, so the poller's work is the
    # arrangement every one of these pages is read against.
    decks.refresh()
    lobby = create_lobby(
        identity=Identity(
            users=FakeUserStore(),
            sessions=FakeSessionRecordStore(),
            attempts=FakeLoginAttemptStore(),
            hasher=ReversibleHasher(),
            clock=clock,
            identifiers=CountingIdentifierFactory(),
            cookies=MarkingCookieSigner(),
        ),
        decks=decks,
        wording=Wording(
            text=_TEXT,
            age_in_words=partial(age_in_words, language_tag=_TEXT.language_tag),
        ),
        secure_cookies=False,
        fetch_hook=None,
    )
    client = TestClient(lobby, follow_redirects=False)
    client.post(
        "/setup",
        data={
            "username": _PERSON,
            "password": _TYPED_WORDS,
            "repeated_password": _TYPED_WORDS,
        },
    )
    return client


def a_configured_source() -> Source:
    return Source(
        url=_ADDRESS,
        ref="main",
        credential_reference=None,
        owner_id="the-admin",
    )


@pytest.fixture
def empty_lobby() -> TestClient:
    return a_signed_in_lobby(source=a_configured_source())


def test_a_source_without_a_deck_names_the_git_address_instead_of_a_table(
    empty_lobby: TestClient,
) -> None:
    listed = empty_lobby.get("/")

    assert listed.status_code == HTTPStatus.OK
    assert _TEXT.decks_empty_title in listed.text
    assert _TEXT.decks_empty_explanation in listed.text
    assert _ADDRESS in listed.text
    assert "<table" not in listed.text


def test_an_empty_list_offers_no_way_to_add_a_deck_or_a_source(
    empty_lobby: TestClient,
) -> None:
    listed = empty_lobby.get("/")

    assert "<input" not in listed.text
    assert "Add source" not in listed.text


def test_a_pushed_deck_is_listed_with_its_title_its_folder_and_its_age() -> None:
    lobby = a_signed_in_lobby(
        a_folder(
            "kundenfeedback",
            title="Kundenfeedback Q3",
            changed_ago=timedelta(minutes=2),
        ),
        source=a_configured_source(),
    )

    listed = lobby.get("/").text

    assert 'href="/deck/kundenfeedback"' in listed
    assert "Kundenfeedback Q3" in listed
    assert "2 minutes ago" in listed
    assert _TEXT.decks_column_changed in listed


def test_the_most_recently_changed_deck_stands_at_the_top_of_the_list() -> None:
    lobby = a_signed_in_lobby(
        a_folder("older", title="Older talk", changed_ago=timedelta(days=6)),
        a_folder("newer", title="Newer talk", changed_ago=timedelta(minutes=2)),
        source=a_configured_source(),
    )

    listed = lobby.get("/").text

    assert listed.index("Newer talk") < listed.index("Older talk")
    assert "6 days ago" in listed


def test_the_list_page_marks_the_decks_section_as_the_current_one(
    empty_lobby: TestClient,
) -> None:
    listed = empty_lobby.get("/").text

    assert f'aria-current="page">{_TEXT.section_decks}<' in listed


def test_an_instance_without_a_source_still_says_where_decks_belong() -> None:
    lobby = a_signed_in_lobby()

    listed = lobby.get("/").text

    assert _TEXT.decks_empty_explanation in listed
    assert "<code>" not in listed
