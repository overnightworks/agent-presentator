"""The service reads the deck folder, not a hand-typed slide list."""

from __future__ import annotations

from pathlib import Path

import pytest

from copresenter.deck import DeckNotFoundError, load_deck

from .conftest import EXAMPLE_DECK


def test_load_deck_reads_slides_and_speaker_notes() -> None:
    deck = load_deck(EXAMPLE_DECK, language="de")

    assert deck.title == "Co-presenter"
    assert deck.language == "de"
    assert len(deck.slides) >= 2
    first = deck.at(1)
    assert first.title
    assert first.notes
    second = deck.at(2)
    assert second.number == 2


def test_load_deck_refuses_a_folder_without_slides(tmp_path: Path) -> None:
    with pytest.raises(DeckNotFoundError):
        load_deck(tmp_path)
