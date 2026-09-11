"""Provider-neutral values carried by the authenticated co-presenter path."""

from dataclasses import dataclass


class CoPresenterUnavailableError(RuntimeError):
    """The private service refused or could not satisfy an operation."""

    def __str__(self) -> str:
        """Keep the expected failure provider-neutral."""
        return "co-presenter unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class Question:
    """What a person asked while showing one slide."""

    said: str
    slide: int
    language: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CoPresenterReadiness:
    """The three readiness facts the repository overlay consumes."""

    answerer_model: str
    hearing_sample_rate: int
    local_hearing_ready: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerText:
    """One incremental answer fragment."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Sentence:
    """One complete answer sentence."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Audio:
    """The spoken WAV for one answer sentence."""

    text: str
    wav: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class Done:
    """The complete answer text."""

    text: str


@dataclass(frozen=True, slots=True)
class AnswerUnavailable:
    """A terminal expected answer failure."""


type AnswerEvent = AnswerText | Sentence | Audio | Done | AnswerUnavailable


@dataclass(frozen=True, slots=True, kw_only=True)
class HearingTranscript:
    """One interim or final transcript from private hearing."""

    text: str
    final: bool


@dataclass(frozen=True, slots=True)
class HearingUnavailable:
    """The private hearing child ended with an expected refusal."""
