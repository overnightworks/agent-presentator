"""A spoken answer releases only whole sentences."""

import re

import pytest

from presentator.application.spoken_answer import SpokenAnswer


def split_after_terminators(text: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"(?<=[.!?])\s+", text) if part)


def spoken(*deltas: str) -> list[str]:
    answer = SpokenAnswer(split_after_terminators)
    sentences = [s for delta in deltas for s in answer.feed(delta)]
    return [*sentences, *answer.flush()]


def test_sentence_waits_for_content_after_its_terminator() -> None:
    answer = SpokenAnswer(split_after_terminators)
    assert answer.feed("Hallo Welt.") == ()
    assert answer.feed(" Wie") == ("Hallo Welt.",)


@pytest.mark.parametrize(
    ("deltas", "sentences"),
    [
        (("Eins. ", "Zwei! ", "Drei?"), ["Eins.", "Zwei!", "Drei?"]),
        (("Eins. Zw", "ei. Drei"), ["Eins.", "Zwei.", "Drei"]),
        (("Ohne Ende",), ["Ohne Ende"]),
        ((), []),
        (("",), []),
    ],
)
def test_token_sequence_emits_whole_sentences_and_flush_ends_it(
    deltas: tuple[str, ...], sentences: list[str]
) -> None:
    assert spoken(*deltas) == sentences


def test_flush_empties_the_answer() -> None:
    answer = SpokenAnswer(split_after_terminators)
    answer.feed("Rest")
    answer.flush()
    assert answer.flush() == ()


@pytest.mark.parametrize("line", ["- Punkt eins ohne Ende\n", "am 3. ", "wort "])
def test_unterminated_stream_hands_the_boundaries_linear_text(line: str) -> None:
    handed_over = 0

    def counting_boundaries(text: str) -> tuple[str, ...]:
        nonlocal handed_over
        handed_over += len(text)
        return split_after_terminators(text)

    answer = SpokenAnswer(counting_boundaries)
    text = (line * 1000)[:10_000]
    for character in text:
        answer.feed(character)

    assert handed_over < 100 * len(text)
