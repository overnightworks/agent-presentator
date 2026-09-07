"""The deck list as a browser receives it."""

from datetime import timedelta
from http import HTTPStatus
from typing import Final

import pytest
from fastapi.testclient import TestClient

from presentator.contracts.decks import MANIFEST_FILE, SLIDES_FILE, DeckFolder
from tests.api.lobby import (
    ENGLISH,
    NOW,
    GivenDecks,
    a_configured_source,
    a_signed_in_lobby,
)

_ADDRESS: Final = "git@heimserver:decks.git"
_A_DECK: Final = frozenset({MANIFEST_FILE, SLIDES_FILE})
_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"


def a_folder(name: str, *, title: str, changed_ago: timedelta) -> DeckFolder:
    return DeckFolder(
        name=name,
        file_names=_A_DECK,
        title=title,
        changed_at=NOW - changed_ago,
        commit=_COMMIT,
    )


def the_page_itself(page: str) -> str:
    """What the page shows below the header every signed-in page carries."""
    return page[page.index("<main") :]


@pytest.fixture
def empty_lobby() -> TestClient:
    return a_signed_in_lobby(GivenDecks(source=a_configured_source(_ADDRESS)))


def test_a_source_without_a_deck_names_the_git_address_instead_of_a_table(
    empty_lobby: TestClient,
) -> None:
    listed = empty_lobby.get("/")

    assert listed.status_code == HTTPStatus.OK
    assert ENGLISH.decks_empty_title in listed.text
    assert ENGLISH.decks_empty_explanation in listed.text
    assert _ADDRESS in listed.text
    assert "<table" not in listed.text


def test_an_empty_list_offers_no_way_to_add_a_deck_or_a_source(
    empty_lobby: TestClient,
) -> None:
    listed = the_page_itself(empty_lobby.get("/").text)

    assert "<input" not in listed
    assert "Add source" not in listed


def test_a_pushed_deck_is_listed_with_its_title_its_folder_and_its_age() -> None:
    lobby = a_signed_in_lobby(
        GivenDecks(
            folders=(
                a_folder(
                    "kundenfeedback",
                    title="Kundenfeedback Q3",
                    changed_ago=timedelta(minutes=2),
                ),
            ),
            source=a_configured_source(_ADDRESS),
        ),
    )

    listed = lobby.get("/").text

    assert 'href="/deck/kundenfeedback"' in listed
    assert "Kundenfeedback Q3" in listed
    assert "2 minutes ago" in listed
    assert ENGLISH.decks_column_changed in listed


def test_the_most_recently_changed_deck_stands_at_the_top_of_the_list() -> None:
    lobby = a_signed_in_lobby(
        GivenDecks(
            folders=(
                a_folder("older", title="Older talk", changed_ago=timedelta(days=6)),
                a_folder("newer", title="Newer talk", changed_ago=timedelta(minutes=2)),
            ),
            source=a_configured_source(_ADDRESS),
        ),
    )

    listed = lobby.get("/").text

    assert listed.index("Newer talk") < listed.index("Older talk")
    assert "6 days ago" in listed


def test_the_list_page_marks_the_decks_section_as_the_current_one(
    empty_lobby: TestClient,
) -> None:
    listed = empty_lobby.get("/").text

    assert f'aria-current="page">{ENGLISH.section_decks}<' in listed


def test_an_instance_without_a_source_still_says_where_decks_belong() -> None:
    lobby = a_signed_in_lobby()

    listed = lobby.get("/").text

    assert ENGLISH.decks_empty_explanation in listed
    assert "<code>" not in listed
