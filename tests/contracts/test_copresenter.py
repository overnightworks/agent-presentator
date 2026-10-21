"""The neutral values shared by the co-presenter path."""

from dataclasses import FrozenInstanceError

import pytest

from presentator.contracts.copresenter import (
    AnswerText,
    AnswerUnavailable,
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    Done,
    HearingTranscript,
    Question,
    Sentence,
)


def test_copresenter_values_are_closed_and_immutable() -> None:
    question = Question(said="Was ist wichtig?", slide=3, language="de")
    readiness = CoPresenterReadiness(
        hearing_sample_rate=16_000,
        local_hearing_ready=True,
    )
    events = (
        AnswerText(text="Ein"),
        Sentence(text="Ein Satz."),
        Audio(text="Ein Satz.", wav=b"RIFF"),
        Done(text="Ein Satz."),
        AnswerUnavailable(),
        HearingTranscript(text="eine Frage", final=True),
    )

    assert question == Question(said="Was ist wichtig?", slide=3, language="de")
    assert readiness.local_hearing_ready is True
    assert events[-1] == HearingTranscript(text="eine Frage", final=True)
    with pytest.raises(FrozenInstanceError):
        question.slide = 4  # type: ignore[misc]


def test_expected_private_refusal_has_one_neutral_exception() -> None:
    assert str(CoPresenterUnavailableError()) == "co-presenter unavailable"
