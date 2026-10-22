"""German sentence boundaries."""

import pytest

from presentator.adapters.sentences import split_sentences


@pytest.mark.parametrize(
    ("text", "sentences"),
    [
        ("Das ist Dr. Müller. Er kommt.", ("Das ist Dr. Müller.", "Er kommt.")),
        ("Er kommt z. B. morgen. Ja.", ("Er kommt z. B. morgen.", "Ja.")),
        ("Er sagte 3. Dann ging er.", ("Er sagte 3.", "Dann ging er.")),
        ("Gut! Wirklich? Ja.", ("Gut!", "Wirklich?", "Ja.")),
        ("Hallo. Wie geht", ("Hallo.", "Wie geht")),
        ("", ()),
    ],
)
def test_text_splits_into_german_sentences(
    text: str, sentences: tuple[str, ...]
) -> None:
    assert split_sentences(text) == sentences
