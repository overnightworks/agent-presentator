"""The neutral values shared by the co-presenter path."""

from dataclasses import FrozenInstanceError

import pytest

from presentator.contracts.copresenter import (
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    PersonToken,
    Question,
)


def test_copresenter_values_are_closed_and_immutable() -> None:
    question = Question(said="Was ist wichtig?", slide=3, language="de")
    readiness = CoPresenterReadiness(
        hearing_sample_rate=16_000,
        local_hearing_ready=True,
    )
    assert question == Question(said="Was ist wichtig?", slide=3, language="de")
    assert readiness.local_hearing_ready is True
    with pytest.raises(FrozenInstanceError):
        question.slide = 4  # type: ignore[misc]


def test_expected_private_refusal_has_one_neutral_exception() -> None:
    assert str(CoPresenterUnavailableError()) == "co-presenter unavailable"


def test_a_person_token_shows_no_value_in_repr_or_str() -> None:
    token = PersonToken("person-token-MARKER")

    assert "person-token-MARKER" not in f"{token!r} {token!s}"
    assert token.value == "person-token-MARKER"
