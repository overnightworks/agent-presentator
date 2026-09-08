"""Streaming text is split into sentences as soon as each one is complete."""

from __future__ import annotations

from copresenter.sentences import flush, take_sentences


def test_take_sentences_holds_a_terminator_at_the_end_of_the_buffer() -> None:
    done, rest = take_sentences("Hello there.")

    assert done == []
    assert rest == "Hello there."


def test_take_sentences_emits_a_sentence_once_whitespace_follows() -> None:
    done, rest = take_sentences("Hello there. More")

    assert done == ["Hello there."]
    assert rest == "More"


def test_flush_returns_the_unfinished_tail() -> None:
    assert flush("  last bit  ") == "last bit"


def test_two_sentences_split_as_the_buffer_grows() -> None:
    buffer = ""
    spoken: list[str] = []
    for chunk in ["Du hast ", "gefragt. Wir ", "sind hier."]:
        buffer += chunk
        done, buffer = take_sentences(buffer)
        spoken.extend(done)
    leftover = flush(buffer)
    if leftover:
        spoken.append(leftover)

    assert spoken == ["Du hast gefragt.", "Wir sind hier."]
