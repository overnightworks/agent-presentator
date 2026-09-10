"""The adapter speaks the maintained SSE and WebSocket protocols over one UDS."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse

from presentator.adapters.copresenter import UdsCoPresenter
from presentator.contracts.copresenter import (
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailable,
    Done,
    HearingTranscript,
    Question,
    Text,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def test_uds_adapter_maps_narrow_readiness_and_native_incremental_sse(
    tmp_path: Path,
) -> None:
    asyncio.run(_read_readiness_and_answer(tmp_path))


async def _read_readiness_and_answer(tmp_path: Path) -> None:
    seen_headers: dict[str, str] = {}
    app = FastAPI()

    async def _who(request: Request) -> dict[str, object]:
        seen_headers.update(request.headers)
        return {
            "answerer": {"provider": "private", "model": "canned"},
            "speech": {
                "sample_rate": 16_000,
                "hearing": {"ready": True, "model": "private-hearing"},
                "credential": "unused",
            },
            "deck": {"path": "/private/deck"},
        }

    async def _ask(request: Request) -> StreamingResponse:
        seen_headers.update(request.headers)
        assert await request.json() == {"said": "Frage", "slide": 1, "language": "de"}

        async def events() -> AsyncIterator[bytes]:
            yield b'event: text\ndata: {"text":\ndata: "Antwort"}\n\n'
            yield b'event: audio\ndata: {"text":"Antwort","wav_b64":"UklGRg=="}\n\n'
            yield b'event: done\ndata: {"text":"Antwort"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_api_route("/who", _who, methods=["GET"])
    app.add_api_route("/ask", _ask, methods=["POST"])

    socket_path = tmp_path / "copresenter.sock"
    async with _Serving(app, socket_path):
        adapter = UdsCoPresenter(socket_path)
        readiness = await adapter.readiness()
        async with adapter.answer(
            Question(said="Frage", slide=1, language="de")
        ) as answer:
            events = [event async for event in answer]

    assert readiness == CoPresenterReadiness(
        answerer_model="canned",
        hearing_sample_rate=16_000,
        local_hearing_ready=True,
    )
    assert events == [
        Text(text="Antwort"),
        Audio(text="Antwort", wav=b"RIFF"),
        Done(text="Antwort"),
    ]
    assert "cookie" not in seen_headers
    assert "authorization" not in seen_headers
    assert "x-csrf-token" not in seen_headers


def test_uds_adapter_uses_native_websocket_in_both_directions(tmp_path: Path) -> None:
    asyncio.run(_use_hearing(tmp_path))


async def _use_hearing(tmp_path: Path) -> None:
    app = FastAPI()
    received: list[bytes] = []

    async def _hear(socket: WebSocket, language: str) -> None:
        assert language == "de"
        await socket.accept()
        received.append(await socket.receive_bytes())
        await socket.send_json({"text": "gehört", "final": True})

    app.add_api_websocket_route("/hear", _hear)

    socket_path = tmp_path / "copresenter.sock"
    async with _Serving(app, socket_path):
        adapter = UdsCoPresenter(socket_path)
        async with adapter.hear("de") as hearing:
            await hearing.send_pcm(b"pcm")
            transcript = await hearing.receive()

    assert received == [b"pcm"]
    assert transcript == HearingTranscript(text="gehört", final=True)


def test_missing_socket_maps_to_typed_unavailability_without_tcp_retry(
    tmp_path: Path,
) -> None:
    async def refused() -> None:
        with pytest.raises(CoPresenterUnavailable):
            await UdsCoPresenter(tmp_path / "missing.sock").readiness()

    asyncio.run(refused())


class _Serving:
    def __init__(self, app: FastAPI, path: Path) -> None:
        self._app = app
        self._path = path
        self._listener: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self._path))
        listener.listen()
        listener.settimeout(0.0)
        server = uvicorn.Server(
            uvicorn.Config(self._app, log_level="error", proxy_headers=False),
        )
        self._listener = listener
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[listener]))

    async def __aexit__(self, *_error: object) -> None:
        assert self._server is not None
        assert self._task is not None
        assert self._listener is not None
        self._server.should_exit = True
        await self._task
        self._listener.close()
