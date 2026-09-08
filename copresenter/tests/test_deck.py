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
    assert len(deck.slides) == 3
    first = deck.at(1)
    assert first.title
    assert first.notes
    second = deck.at(2)
    assert second.number == 2


def test_load_deck_refuses_a_folder_without_slides(tmp_path: Path) -> None:
    with pytest.raises(DeckNotFoundError):
        load_deck(tmp_path)


def test_load_deck_does_not_count_per_slide_frontmatter_as_a_slide(tmp_path: Path) -> None:
    folder = tmp_path / "talk"
    folder.mkdir()
    (folder / "slides.md").write_text(
        "---\n"
        "title: Two\n"
        "---\n"
        "\n"
        "# First\n"
        "\n"
        "Hello\n"
        "\n"
        "---\n"
        "layout: center\n"
        "---\n"
        "\n"
        "# Second\n"
        "\n"
        "World\n",
        encoding="utf-8",
    )

    deck = load_deck(folder)

    assert len(deck.slides) == 2
    assert deck.at(1).title == "First"
    assert deck.at(2).title == "Second"
    assert "layout" not in deck.at(2).body
    assert "World" in deck.at(2).body
