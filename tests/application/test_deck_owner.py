"""A deck exists only for the account that owns its source."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from presentator.application.decks import Decks
from presentator.contracts.decks import DeckPage
from tests.application.test_decks import (
    a_folder,
    a_source,
    carrying,
    decks_over,
    having,
)

_OWNER: Final = "the account that set the instance up"
_OTHER: Final = "another account"
_OWNED = replace(a_source("decks", identifier="the-owners-source"), owner_id=_OWNER)
_FOREIGN = replace(a_source("talks", identifier="the-others-source"), owner_id=_OTHER)


def _built_for_the_owner() -> Decks:
    decks = decks_over(
        mirror=carrying(a_folder("kundenfeedback"), source=_OWNED),
        sources=having(_OWNED),
    )
    decks.refresh()
    return decks


def test_the_list_holds_only_the_decks_the_viewers_sources_own() -> None:
    mirror = carrying(a_folder("mine"), source=_OWNED)
    mirror.carried[_FOREIGN.id] = (a_folder("theirs"),)
    decks = decks_over(mirror=mirror, sources=having(_OWNED, _FOREIGN))
    decks.refresh()

    assert [deck.slug for deck in decks.listed(viewer=_OWNER)] == ["mine"]
    assert [deck.slug for deck in decks.listed(viewer=_OTHER)] == ["theirs"]


def test_a_second_source_with_the_same_folder_name_leaves_the_deck_to_the_first() -> (
    None
):
    mirror = carrying(a_folder("shared"), source=_OWNED)
    mirror.carried[_FOREIGN.id] = (a_folder("shared"),)
    decks = decks_over(mirror=mirror, sources=having(_OWNED, _FOREIGN))
    decks.refresh()

    assert [deck.slug for deck in decks.listed(viewer=_OWNER)] == ["shared"]
    assert decks.listed(viewer=_OTHER) == ()


def _page(decks: Decks, viewer: str) -> DeckPage | None:
    return decks.page("kundenfeedback", viewer=viewer)


def _talk(decks: Decks, viewer: str) -> Path | None:
    return decks.built_talk("kundenfeedback", viewer=viewer)


def _pdf(decks: Decks, viewer: str) -> Path | None:
    return decks.exported_pdf("kundenfeedback", viewer=viewer)


@pytest.mark.parametrize("read", [_page, _talk, _pdf], ids=["page", "talk", "pdf"])
def test_a_deck_is_read_by_its_owner_and_by_nobody_else(
    read: Callable[[Decks, str], DeckPage | Path | None],
) -> None:
    decks = _built_for_the_owner()

    assert read(decks, _OWNER) is not None
    assert read(decks, _OTHER) is None


def test_the_empty_list_names_only_the_address_of_the_viewers_own_single_source() -> (
    None
):
    decks = decks_over(sources=having(_OWNED, _FOREIGN))

    assert decks.source_address(viewer=_OWNER) == _OWNED.url
    assert decks.source_address(viewer=_OTHER) == _FOREIGN.url
    assert decks.source_address(viewer="an account with no source") is None


def test_the_address_is_withheld_from_an_account_whose_source_is_not_alone() -> None:
    decks = decks_over(sources=having(_OWNED, replace(_FOREIGN, owner_id=_OWNER)))

    assert decks.source_address(viewer=_OWNER) is None


def test_a_source_page_offers_only_the_decks_the_viewer_owns() -> None:
    decks = _built_for_the_owner()

    owners = decks.shown_source(_OWNED.name, viewer=_OWNER)
    other = decks.shown_source(_OWNED.name, viewer=_OTHER)

    assert owners is not None
    assert [deck.slug for deck in owners.decks] == ["kundenfeedback"]
    assert other is not None
    assert other.decks == ()
