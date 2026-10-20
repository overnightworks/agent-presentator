"""`POST /ask` streams text, then speaks each finished sentence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
from agent_providers.errors import (
    ContainmentLost,
    ProviderUnavailableError,
    RetainedGroup,
    SafeRouteReasonCode,
    normalize_route_failure,
)
from fastapi.testclient import TestClient

from copresenter.answer import CannedAnswerer, user_message
from copresenter.app import create_app
from copresenter.config import Settings
from copresenter.deck import Deck
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


class FailingAnswerer(CannedAnswerer):
    """An answerer whose Claude turn ends in the given provider failure after a first word."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def stream(self, *, said: str, slide: int, deck: Deck) -> AsyncIterator[str]:
        yield "Das ist"
        raise self.failure


def ask_and_read_body(example_deck, fake_speech, failure: Exception) -> str:
    app = create_app(
        Settings(allowed_origin=ALLOWED_ORIGIN, deck=example_deck.source.parent),
        deck=example_deck,
        speech=fake_speech,
        answerer=FailingAnswerer(failure),
    )
    with TestClient(app, headers={"Origin": ALLOWED_ORIGIN}).stream(
        "POST",
        "/ask",
        json={"said": "Was steht hier?", "slide": 1, "language": "de"},
    ) as response:
        return "".join(response.iter_text())


def test_ask_answers_with_the_error_event_and_names_the_lost_group_in_the_log(
    example_deck,
    fake_speech,
    caplog,
) -> None:
    caplog.set_level(logging.ERROR, logger="copresenter")

    body = ask_and_read_body(
        example_deck,
        fake_speech,
        ContainmentLost(RetainedGroup(pgid=424242, generation=1)),
    )

    assert "event: error" in body
    assert "event: done" not in body
    (record,) = [entry for entry in caplog.records if entry.levelno == logging.ERROR]
    assert "424242" in record.getMessage()
    assert "restart presentator-copresenter.service" in record.getMessage()


def test_ask_answers_with_the_error_event_when_the_provider_refuses_the_turn(
    example_deck,
    fake_speech,
) -> None:
    refusal = ProviderUnavailableError(
        "claude",
        "cli",
        normalize_route_failure(SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED),
    )

    body = ask_and_read_body(example_deck, fake_speech, refusal)

    assert "event: error" in body
    assert "event: done" not in body


def test_ask_reports_an_empty_speech_response_instead_of_an_empty_audio_event(example_deck) -> None:
    asyncio.run(_ask_with_empty_speech_response(example_deck))


def test_ask_keeps_the_partial_answer_when_speech_is_refused_during_a_switch(
    example_deck,
) -> None:
    asyncio.run(_ask_with_refused_speech(example_deck))


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


async def _ask_with_refused_speech(example_deck) -> None:
    speech_transport = httpx.MockTransport(lambda _request: httpx.Response(503))
    async with httpx.AsyncClient(
        transport=speech_transport,
        base_url="http://speech",
    ) as speech_client:
        app = create_app(
            Settings(
                allowed_origin=ALLOWED_ORIGIN,
                deck=example_deck.source.parent,
                speech_url="http://speech",
            ),
            deck=example_deck,
            speech=LocalSpeech("http://speech", client=speech_client),
            answerer=CannedAnswerer(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://copresenter",
            headers={"Origin": ALLOWED_ORIGIN},
        ) as client:
            response = await client.post(
                "/ask",
                json={"said": "Was steht auf dieser Folie?", "slide": 1, "language": "de"},
            )

    assert response.status_code == 200
    assert "event: sentence" in response.text
    assert "event: error" in response.text
    assert "event: audio" not in response.text
