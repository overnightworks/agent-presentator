"""A tiny speech service that honours the issue 74 contract with canned audio.

Not a model. Used so the co-presenter can be proven while the real speech
service is still being built.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from copresenter.wav import SAMPLE_RATE, tone

_log = logging.getLogger("copresenter.standin")
_HEARD = "Was steht auf dieser Folie?"
_CHUNK = 2048


class SpeakRequest(BaseModel):
    """One sentence to speak, as the real service expects it."""

    text: str = Field(min_length=1)
    language: str = "de"


def create_standin() -> FastAPI:
    """Answer `/health`, `/speak`, and `/hear` with canned audio and text."""
    app = FastAPI(title="speech-standin")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "speaking": {"model": "stand-in", "ready": True},
            "hearing": {"model": "stand-in", "ready": True},
            "sample_rate": SAMPLE_RATE,
            "card_memory_mb": 0,
        }

    @app.post("/speak")
    async def speak(request: SpeakRequest) -> StreamingResponse:
        frequency = 440.0 if request.language == "de" else 520.0
        body = tone(seconds=0.4, frequency=frequency)
        return StreamingResponse(_chunks(body), media_type="audio/wav")

    @app.websocket("/hear")
    async def hear(socket: WebSocket, language: str = "de") -> None:
        await socket.accept()
        uttered = False
        try:
            while True:
                message = await socket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") and not uttered:
                    _log.info("stand-in heard a frame language=%s", language)
                    await socket.send_json({"text": _HEARD[:-1], "final": False})
                    await socket.send_json({"text": _HEARD, "final": True})
                    uttered = True
        except WebSocketDisconnect:
            return

    return app


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(body), _CHUNK):
        yield body[start : start + _CHUNK]


def main() -> None:
    """Serve the stand-in on COPRESENTER_STANDIN_PORT, default 8765."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    host = os.environ.get("COPRESENTER_STANDIN_HOST", "127.0.0.1")
    port = int(os.environ.get("COPRESENTER_STANDIN_PORT", "8765"))
    _log.info("speech stand-in on %s:%s", host, port)
    uvicorn.run(create_standin(), host=host, port=port)


if __name__ == "__main__":
    main()
