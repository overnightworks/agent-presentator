"""Shared fakes for the co-presenter suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copresenter.answer import CannedAnswerer
from copresenter.app import create_app
from copresenter.config import Settings
from copresenter.deck import Deck, load_deck
from copresenter.speech import SpeechHealth, SpeechModel
from copresenter.wav import tone

EXAMPLE_DECK = Path(__file__).resolve().parents[2] / "examples" / "copresenter-deck"
ALLOWED_ORIGIN = "https://talk.test"


class RecordingAnswerer(CannedAnswerer):
    """The canned answerer, counting the turns it was actually asked for."""

    def __init__(self) -> None:
        self.turns = 0

    async def stream(self, *, said: str, slide: int, deck: Deck) -> AsyncIterator[str]:
        """Count this turn, then answer as the canned answerer does."""
        self.turns += 1
        async for chunk in super().stream(said=said, slide=slide, deck=deck):
            yield chunk


class FakeSpeech:
    """A speech port that records every call the service makes to it."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []
        self.health_asks = 0
        self.hear_opens = 0
        self.health_value = SpeechHealth(
            speaking=SpeechModel(model="stand-in", ready=True),
            hearing=SpeechModel(model="stand-in", ready=True),
            sample_rate=16000,
            card_memory_mb=0,
            reachable=True,
        )

    async def health(self) -> SpeechHealth:
        self.health_asks += 1
        return self.health_value

    async def speak(self, text: str, language: str) -> bytes:
        self.spoken.append((text, language))
        return tone(seconds=0.2, frequency=440.0)

    def hear_url(self, language: str) -> str:
        return f"ws://speech.test/hear?language={language}"

    async def open_hear(self, language: str) -> object:
        self.hear_opens += 1
        message = f"unit tests do not open hearing ({language})"
        raise OSError(message)


@pytest.fixture
def example_deck():
    return load_deck(EXAMPLE_DECK, language="de")


@pytest.fixture
def fake_speech():
    return FakeSpeech()


@pytest.fixture
def answerer():
    return RecordingAnswerer()


@pytest.fixture
def app(example_deck, fake_speech, answerer):
    settings = Settings(
        allowed_origin=ALLOWED_ORIGIN,
        deck=EXAMPLE_DECK,
        speech_url="http://speech.test",
    )
    return create_app(
        settings,
        deck=example_deck,
        speech=fake_speech,
        answerer=answerer,
    )


@pytest.fixture
def client(app):
    """A caller from the deck's own origin, which is the only one served."""
    return TestClient(app, headers={"Origin": ALLOWED_ORIGIN})
