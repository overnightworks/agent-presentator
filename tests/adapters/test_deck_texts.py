"""A deck's words, read from the mirror of a real repository at its built commit."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from presentator.adapters.deck_texts import MirroredDeckTexts
from presentator.contracts.copresenter import CoPresenterUnavailableError, DeckText
from presentator.contracts.decks import Build, Deck
from tests.adapters.test_decks import (
    a_build,
    a_deck,
    a_source,
    folders_under,
    mirrors_under,
)
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, GitRemote

if TYPE_CHECKING:
    from presentator.ports.copresenter import DeckTexts

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_TALK = "talk"
_SLIDES = "# One\nfirst\n\n---\n\n# Two\nsecond\n\n<!-- notes for two -->\n"


def built(deck: Deck, build: Build) -> Deck:
    return replace(deck, build=build)


def read_deck(tmp_path: Path, remote: GitRemote, slug: str) -> DeckText | None:
    source = a_source(remote.url)
    folders_under(tmp_path).folders(source)
    deck = built(a_deck(slug), a_build(tmp_path, commit=remote.head))
    texts: DeckTexts = MirroredDeckTexts(mirrors=mirrors_under(tmp_path))
    return texts.text_of(deck, source)


def test_a_built_deck_gives_its_title_language_and_slides_with_notes(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(
        {
            f"{_TALK}/slides.md": _SLIDES,
            f"{_TALK}/deck.toml": 'title = "The Talk"\nlanguage = "en"\n',
        },
        at=_PUSHED_AT,
    )

    text = read_deck(tmp_path, remote, _TALK)

    assert text is not None
    assert (text.title, text.language) == ("The Talk", "en")
    assert [(s.number, s.title, s.notes) for s in text.slides] == [
        (1, "One", ""),
        (2, "Two", "notes for two"),
    ]


def test_the_example_deck_is_titled_by_its_manifest(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    text = read_deck(tmp_path, remote, EXAMPLE_SLUG)

    assert text is not None
    assert text.title == EXAMPLE_TITLE


def test_a_deck_without_a_manifest_is_titled_by_its_first_slide(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({f"{_TALK}/slides.md": _SLIDES}, at=_PUSHED_AT)

    text = read_deck(tmp_path, remote, _TALK)

    assert text is not None
    assert (text.title, text.language) == ("One", None)


def test_a_deck_without_a_build_has_no_words(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({f"{_TALK}/slides.md": _SLIDES}, at=_PUSHED_AT)
    source = a_source(remote.url)

    text = MirroredDeckTexts(mirrors=mirrors_under(tmp_path)).text_of(
        a_deck(_TALK), source
    )

    assert text is None


def test_a_built_commit_without_slides_has_no_words(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({f"{_TALK}/notes.txt": "nothing\n"}, at=_PUSHED_AT)

    assert read_deck(tmp_path, remote, _TALK) is None


def test_a_commit_the_mirror_does_not_hold_is_unavailable(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({f"{_TALK}/slides.md": _SLIDES}, at=_PUSHED_AT)
    source = a_source(remote.url)
    folders_under(tmp_path).folders(source)
    deck = built(a_deck(_TALK), a_build(tmp_path))

    with pytest.raises(CoPresenterUnavailableError):
        MirroredDeckTexts(mirrors=mirrors_under(tmp_path)).text_of(deck, source)


def test_a_deck_whose_manifest_is_unreadable_is_still_read_by_its_slides(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(
        {f"{_TALK}/slides.md": _SLIDES, f"{_TALK}/deck.toml": "not a manifest\n"},
        at=_PUSHED_AT,
    )

    text = read_deck(tmp_path, remote, _TALK)

    assert text is not None
    assert (text.title, text.language) == ("One", None)


def test_a_source_that_no_longer_resolves_is_unavailable(tmp_path: Path) -> None:
    deck = built(a_deck(_TALK), a_build(tmp_path))

    with pytest.raises(CoPresenterUnavailableError):
        MirroredDeckTexts(mirrors=mirrors_under(tmp_path)).text_of(
            deck, a_source("not an address")
        )


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        pytest.param(
            "---\ntheme: dark\n---\n# One\nbody\n\n---\n\n# Two\n",
            [(1, "One"), (2, "Two")],
            id="per-slide frontmatter is not a slide",
        ),
        pytest.param(
            "---\ntheme: dark\n# One\n",
            [(1, "One")],
            id="unclosed frontmatter stays in its slide",
        ),
        pytest.param(
            "# One\n```md\n---\n# not a slide\n```\n---\n\n# Two\n",
            [(1, "One"), (2, "Two")],
            id="a fence inside code is not a separator",
        ),
        pytest.param(
            "# One\n<!--\n---\nnotes\n-->\n\n---\n\n# Two\n",
            [(1, "One"), (2, "Two")],
            id="a fence inside a multiline comment is not a separator",
        ),
        pytest.param(
            "# One\n```\nnever closed\n---\n",
            [(1, "One")],
            id="an unclosed code fence runs to the end",
        ),
        pytest.param(
            "# One\n\n---\n\nplain words\n",
            [(1, "One"), (2, "plain words")],
            id="leading blank lines do not title a slide",
        ),
        pytest.param(
            "# One\n\n---\n\n---\n\n# Three\n",
            [(1, "One"), (2, "Three")],
            id="an empty slide is skipped",
        ),
        pytest.param(
            "# One\n\n---\n",
            [(1, "One")],
            id="a trailing separator opens no slide",
        ),
        pytest.param(
            "just words\n",
            [(1, "just words")],
            id="a slide without a heading is titled by its first line",
        ),
        pytest.param(
            "<!-- only notes -->\n",
            [(1, "Untitled")],
            id="a slide of notes alone is untitled",
        ),
    ],
)
def test_slides_follow_slidev_separators(
    remote: GitRemote,
    tmp_path: Path,
    markdown: str,
    expected: list[tuple[int, str]],
) -> None:
    remote.commit({f"{_TALK}/slides.md": markdown}, at=_PUSHED_AT)

    text = read_deck(tmp_path, remote, _TALK)

    assert text is not None
    assert [(s.number, s.title) for s in text.slides] == expected
