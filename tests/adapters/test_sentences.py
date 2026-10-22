"""German sentence boundaries."""

import pytest

from presentator.adapters.sentences import split_sentences
from presentator.application.spoken_answer import SpokenAnswer


@pytest.mark.parametrize(
    ("text", "sentences"),
    [
        ("Das ist Dr. Müller. Er kommt.", ("Das ist Dr. Müller.", "Er kommt.")),
        ("Er kommt z. B. morgen. Ja.", ("Er kommt z. B. morgen.", "Ja.")),
        ("Am 3. Mai kommt er. Ja.", ("Am 3. Mai kommt er.", "Ja.")),
        ("Er wurde 3. Dann kam er.", ("Er wurde 3. Dann kam er.",)),
        ("Gut! Wirklich? Ja.", ("Gut!", "Wirklich?", "Ja.")),
        ("Hallo. Wie geht", ("Hallo.", "Wie geht")),
        ("", ()),
    ],
)
def test_text_splits_into_german_sentences(
    text: str, sentences: tuple[str, ...]
) -> None:
    assert split_sentences(text) == sentences


def test_streamed_ordinal_is_not_released_as_a_sentence() -> None:
    answer = SpokenAnswer(split_sentences)
    assert answer.feed("Am 3. M") == ()
    assert answer.feed("ai kommt er. Ja") == ("Am 3. Mai kommt er.",)


def test_streamed_abbreviation_is_not_released_as_a_sentence() -> None:
    answer = SpokenAnswer(split_sentences)
    assert answer.feed("Das ist Dr.") == ()
    assert answer.feed(" Müller. Er") == ("Das ist Dr. Müller.",)


@pytest.mark.parametrize("gap", ["\n", "  ", "\n\n"])
def test_streamed_ordinal_followed_by_any_whitespace_is_released_whole(
    gap: str,
) -> None:
    answer = SpokenAnswer(split_sentences)
    assert answer.feed(f"Am 3.{gap}Mai kommt er. Ja") == (f"Am 3.{gap}Mai kommt er.",)
