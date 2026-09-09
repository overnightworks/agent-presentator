"""`POST /ask` streams text, then speaks each finished sentence."""

from __future__ import annotations

import asyncio

import httpx

from copresenter.answer import CannedAnswerer, user_message
from copresenter.app import create_app
from copresenter.config import Settings
from copresenter.speech import LocalSpeech

from .conftest import ALLOWED_ORIGIN


def test_user_message_gives_claude_the_current_slide_and_the_deck(example_deck) -> None:
    message = user_message("Was steht hier?", 1, example_deck)

    assert "CURRENT SLIDE" in message
    assert example_deck.at(1).title in message
    assert example_deck.at(1).notes in message
    assert "DECK" in message
    assert example_deck.at(2).title in message
    assert "Was steht hier?" in message


def test_ask_streams_sentences_and_audio(client, fake_speech) -> None:
    with client.stream(
        "POST",
        "/ask",
        json={"said": "Was steht auf dieser Folie?", "slide": 1, "language": "de"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: sentence" in body
    assert "event: audio" in body
    assert "event: done" in body
    assert "wav_b64" in body
    assert fake_speech.spoken
    assert all(language == "de" for _text, language in fake_speech.spoken)
    assert "Folie 1" in body


def test_ask_reports_an_empty_speech_response_instead_of_an_empty_audio_event(example_deck) -> None:
    asyncio.run(_ask_with_empty_speech_response(example_deck))


async def _ask_with_empty_speech_response(example_deck) -> None:
    speech_transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b""))
    async with httpx.AsyncClient(
        transport=speech_transport,
        base_url="http://speech",
    ) as speech_client:
        speech = LocalSpeech("http://speech", client=speech_client)
        app = create_app(
            Settings(
                allowed_origin=ALLOWED_ORIGIN,
                deck=example_deck.source.parent,
                speech_url="http://speech",
            ),
            deck=example_deck,
            speech=speech,
            answerer=CannedAnswerer(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://copresenter",
            headers={"Origin": ALLOWED_ORIGIN},
        ) as client:
            response = await client.post(
                "/ask",
                json={"said": "Was steht auf dieser Folie?", "slide": 1, "language": "de"},
            )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "event: audio" not in response.text
