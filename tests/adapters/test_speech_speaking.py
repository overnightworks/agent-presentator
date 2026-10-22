"""Speaking one sentence over speech's private socket, against a fake of speech."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from presentator.adapters.speech import UdsSpeaking
from presentator.contracts.copresenter import CoPresenterUnavailableError
from tests.adapters.serving import Serving, unix_listener

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

WAV_CHUNKS = (b"RIFF", b"-first-", b"-second-")


class FakeSpeech:
    """A fake /speak that records requests and notices its connection closing."""

    def __init__(self) -> None:
        """Build the app with its one /speak route."""
        self.requests: list[dict[str, str]] = []
        self.connection_closed = asyncio.Event()
        self.first_chunk_sent = asyncio.Event()
        self.app = FastAPI()
        self.app.add_api_route("/speak", self._speak, methods=["POST"])
        self.body = self._whole_wav

    async def _speak(self, request: Request) -> Response:
        self.requests.append(await request.json())
        return self.body()

    def _whole_wav(self) -> Response:
        return StreamingResponse(self._chunks(WAV_CHUNKS), media_type="audio/wav")

    def stalling(self) -> Response:
        return StreamingResponse(self._stall(), media_type="audio/wav")

    async def _chunks(self, chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk
            await asyncio.sleep(0)

    async def _stall(self) -> AsyncIterator[bytes]:
        try:
            yield b"RIFF"
            self.first_chunk_sent.set()
            await asyncio.sleep(30)
        finally:
            self.connection_closed.set()


async def speak_against(tmp_path: Path, fake: FakeSpeech, **speaking: float) -> bytes:
    socket_path = tmp_path / "speech.sock"
    async with Serving(fake.app, unix_listener(socket_path)):
        return await UdsSpeaking(socket_path, **speaking).speak("Eins.", "de")


def test_speaking_returns_the_whole_wav_and_sends_text_and_language(
    tmp_path: Path,
) -> None:
    fake = FakeSpeech()

    wav = asyncio.run(speak_against(tmp_path, fake))

    assert wav == b"".join(WAV_CHUNKS)
    assert fake.requests == [{"text": "Eins.", "language": "de"}]


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(
            lambda: JSONResponse({"detail": "not ready"}, status_code=503), id="503"
        ),
        pytest.param(lambda: Response(b"", media_type="audio/wav"), id="empty-body"),
    ],
)
def test_a_refusal_or_an_empty_body_is_unavailable(
    tmp_path: Path, refusal: Callable[[], Response]
) -> None:
    fake = FakeSpeech()
    fake.body = refusal

    speaking = speak_against(tmp_path, fake)

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(speaking)


def test_a_missing_socket_is_unavailable(tmp_path: Path) -> None:
    speech = UdsSpeaking(tmp_path / "absent.sock").speak("Eins.", "de")

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(speech)


def test_a_stalled_body_is_unavailable_and_closes_the_connection(
    tmp_path: Path,
) -> None:
    fake = FakeSpeech()
    fake.body = fake.stalling

    async def scenario() -> None:
        with pytest.raises(CoPresenterUnavailableError):
            await speak_against(tmp_path, fake, read_timeout=0.2)
        await asyncio.wait_for(fake.connection_closed.wait(), timeout=5)

    asyncio.run(scenario())


def test_a_cancelled_speak_closes_the_connection_and_raises_nothing_unavailable(
    tmp_path: Path,
) -> None:
    fake = FakeSpeech()
    fake.body = fake.stalling

    async def scenario() -> None:
        task = asyncio.create_task(speak_against(tmp_path, fake))
        await asyncio.wait_for(fake.first_chunk_sent.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(fake.connection_closed.wait(), timeout=5)

    asyncio.run(scenario())
