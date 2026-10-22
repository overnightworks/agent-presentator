"""A spoken answer releases only whole sentences."""

import asyncio
import re
from collections.abc import AsyncIterator

import pytest

from presentator.application.spoken_answer import (
    SpokenAnswer,
    spoken_events,
    spoken_language,
)
from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerText,
    Audio,
    CoPresenterUnavailableError,
    Done,
    Sentence,
)


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
        (("Eins. Zwei ", "  "), ["Eins.", "Zwei"]),
        (("  ",), []),
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


class RecordingSpeaking:
    """A Speaking fake that records its calls and can fail on the nth."""

    def __init__(self, fail_on: int | None = None) -> None:
        """Fail on the nth call when given."""
        self.calls: list[tuple[str, str]] = []
        self._fail_on = fail_on

    async def speak(self, text: str, language: str) -> bytes:
        self.calls.append((text, language))
        if self._fail_on == len(self.calls):
            raise CoPresenterUnavailableError
        return f"wav:{text}".encode()


async def _deltas(*deltas: str) -> AsyncIterator[str]:
    for delta in deltas:
        yield delta


async def _events(
    deltas: tuple[str, ...], speaking: RecordingSpeaking, events: list[AnswerEvent]
) -> None:
    async for event in spoken_events(
        _deltas(*deltas), split_after_terminators, speaking, "de"
    ):
        # Events must land one by one so a failing stream keeps the earlier ones.
        events.append(event)  # noqa: PERF401


def test_each_sentence_is_spoken_once_in_order_and_done_carries_the_sentences() -> None:
    speaking = RecordingSpeaking()
    events: list[AnswerEvent] = []

    asyncio.run(_events(("Eins. Zw", "ei. Drei"), speaking, events))

    assert events == [
        AnswerText(text="Eins. Zw"),
        Sentence(text="Eins."),
        Audio(text="Eins.", wav=b"wav:Eins."),
        AnswerText(text="ei. Drei"),
        Sentence(text="Zwei."),
        Audio(text="Zwei.", wav=b"wav:Zwei."),
        Sentence(text="Drei"),
        Audio(text="Drei", wav=b"wav:Drei"),
        Done(text="Eins. Zwei. Drei"),
    ]
    assert speaking.calls == [("Eins.", "de"), ("Zwei.", "de"), ("Drei", "de")]


@pytest.mark.parametrize("deltas", [(), ("  ",)])
def test_no_text_speaks_nothing_and_ends_with_empty_done(
    deltas: tuple[str, ...],
) -> None:
    speaking = RecordingSpeaking()
    events: list[AnswerEvent] = []

    asyncio.run(_events(deltas, speaking, events))

    assert events[-1] == Done(text="")
    assert speaking.calls == []


def test_a_speech_failure_propagates_after_earlier_sentences_without_done() -> None:
    speaking = RecordingSpeaking(fail_on=2)
    events: list[AnswerEvent] = []

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(_events(("Eins. Zwei. Drei",), speaking, events))

    assert Audio(text="Eins.", wav=b"wav:Eins.") in events
    assert not any(isinstance(event, Done) for event in events)


@pytest.mark.parametrize(
    ("asked", "deck", "language"),
    [
        ("en", "fr", "en"),
        ("en", None, "en"),
        (None, "fr", "fr"),
        (None, None, "de"),
        ("en", "en", "en"),
        (None, "de", "de"),
    ],
)
def test_the_asked_language_wins_over_the_decks_then_german(
    asked: str | None, deck: str | None, language: str
) -> None:
    assert spoken_language(asked, deck) == language
