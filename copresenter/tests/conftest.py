"""Shared fakes for the co-presenter suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from copresenter.answer import CannedAnswerer
from copresenter.app import create_app
from copresenter.config import Settings
from copresenter.deck import load_deck
from copresenter.speech import SpeechHealth, SpeechModel
from copresenter.wav import tone

EXAMPLE_DECK = Path(__file__).resolve().parents[2] / "examples" / "copresenter-deck"
ALLOWED_ORIGIN = "https://talk.test"


class FakeSpeech:
    """A speech port that records what it was asked to say."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []
        self.health_value = SpeechHealth(
            speaking=SpeechModel(model="stand-in", ready=True),
            hearing=SpeechModel(model="stand-in", ready=True),
            sample_rate=16000,
            card_memory_mb=0,
            reachable=True,
        )

    async def health(self) -> SpeechHealth:
        return self.health_value

    async def speak(self, text: str, language: str) -> bytes:
        self.spoken.append((text, language))
        return tone(seconds=0.2, frequency=440.0)

    def hear_url(self, language: str) -> str:
        return f"ws://speech.test/hear?language={language}"

    async def open_hear(self, language: str) -> object:
        message = f"unit tests do not open hearing ({language})"
        raise OSError(message)


@pytest.fixture
def example_deck():
    return load_deck(EXAMPLE_DECK, language="de")


@pytest.fixture
def fake_speech():
    return FakeSpeech()


@pytest.fixture
def app(example_deck, fake_speech):
    settings = Settings(
        allowed_origin=ALLOWED_ORIGIN,
        deck=EXAMPLE_DECK,
        speech_url="http://speech.test",
    )
    return create_app(
        settings,
        deck=example_deck,
        speech=fake_speech,
        answerer=CannedAnswerer(),
    )
