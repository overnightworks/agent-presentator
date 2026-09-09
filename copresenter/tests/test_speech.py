"""The local speech client talks the issue 74 contract, never a model."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from copresenter.speech import LocalSpeech
from copresenter.standin import create_standin


def test_local_speech_reads_health_and_speak_from_the_standin() -> None:
    asyncio.run(_against_standin())


def test_local_speech_refuses_an_empty_successful_speech_response() -> None:
    asyncio.run(_empty_speech_response_is_refused())


async def _against_standin() -> None:
    transport = httpx.ASGITransport(app=create_standin())
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        speech = LocalSpeech("http://speech", client=client)
        health = await speech.health()
        wav = await speech.speak("Hallo.", "de")

    assert health.reachable is True
    assert health.speaking.model == "stand-in"
    assert health.hearing.ready is True
    assert wav[:4] == b"RIFF"
    assert len(wav) > 44
    assert speech.hear_url("de") == "ws://speech/hear?language=de"


async def _empty_speech_response_is_refused() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b""))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        speech = LocalSpeech("http://speech", client=client)

        with pytest.raises(ValueError, match="speech response is empty"):
            await speech.speak("Hallo.", "de")
