"""A spoken answer releases only whole sentences."""

import pytest

from presentator.adapters.sentences import split_sentences
from presentator.application.spoken_answer import SpokenAnswer


def spoken(*deltas: str) -> list[str]:
    answer = SpokenAnswer(split_sentences)
    sentences = [s for delta in deltas for s in answer.feed(delta)]
    return [*sentences, *answer.flush()]


def test_sentence_waits_for_content_after_its_terminator() -> None:
    answer = SpokenAnswer(split_sentences)
    assert answer.feed("Hallo Welt.") == ()
    assert answer.feed(" Wie") == ("Hallo Welt.",)


def test_buffer_ending_in_abbreviation_never_fires() -> None:
    answer = SpokenAnswer(split_sentences)
    assert answer.feed("Das ist Dr.") == ()
    assert answer.feed(" Müller. Er") == ("Das ist Dr. Müller.",)


@pytest.mark.parametrize(
    ("deltas", "sentences"),
    [
        (("Er kommt z. ", "B. morgen. Ja."), ["Er kommt z. B. morgen.", "Ja."]),
        (("Er sagte 3.", " Dann ging er."), ["Er sagte 3.", "Dann ging er."]),
        (("Eins. ", "Zwei! ", "Drei?"), ["Eins.", "Zwei!", "Drei?"]),
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
    answer = SpokenAnswer(split_sentences)
    answer.feed("Rest")
    answer.flush()
    assert answer.flush() == ()
