"""The adapter speaks the maintained SSE and WebSocket protocols over one UDS."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

from presentator.adapters.copresenter import UdsCoPresenter
from presentator.application.copresenter import CoPresenterUse
from presentator.contracts.copresenter import (
    AnswerUnavailable,
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    Done,
    HearingTranscript,
    HearingUnavailable,
    Question,
    Sentence,
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
            yield b'event: sentence\ndata: {"text":"Antwort."}\n\n'
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
        Sentence(text="Antwort."),
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
        async with CoPresenterUse(private=adapter).hear("de") as hearing:
            await hearing.send_pcm(b"pcm")
            transcript = await hearing.receive()

    assert received == [b"pcm"]
    assert transcript == HearingTranscript(text="gehört", final=True)


def test_uds_adapter_maps_private_hearing_endings_to_the_port_contract(
    tmp_path: Path,
) -> None:
    asyncio.run(_read_private_hearing_endings(tmp_path))


async def _read_private_hearing_endings(tmp_path: Path) -> None:
    app = FastAPI()

    async def _hear(socket: WebSocket, language: str) -> None:
        await socket.accept()
        if language == "crashed":
            message = "private peer crashed"
            raise RuntimeError(message)
        if language == "caller-fault":
            await socket.send_json({"text": "opened", "final": True})
            await socket.close()
            return
        if language == "unavailable":
            await socket.send_json({"text": "", "final": True, "error": "unavailable"})
        else:
            await socket.close()

    app.add_api_websocket_route("/hear", _hear)
    socket_path = tmp_path / "copresenter.sock"

    async with _Serving(app, socket_path):
        adapter = UdsCoPresenter(socket_path)
        async with adapter.hear("unavailable") as hearing:
            unavailable = await hearing.receive()
        async with adapter.hear("closed") as hearing:
            closed = await hearing.receive()
        with pytest.raises(CoPresenterUnavailableError):
            async with adapter.hear("crashed") as hearing:
                await hearing.send_pcm(b"pcm")
        with pytest.raises(ExceptionGroup) as propagated:
            await _raise_caller_fault(adapter)

    assert unavailable == HearingUnavailable()
    assert closed is None
    assert propagated.value.subgroup(_CallerFaultError) is not None


def test_missing_socket_maps_to_typed_unavailability_without_tcp_retry(
    tmp_path: Path,
) -> None:
    async def refused() -> None:
        adapter = UdsCoPresenter(tmp_path / "missing.sock")
        with pytest.raises(CoPresenterUnavailableError):
            await adapter.readiness()
        with pytest.raises(CoPresenterUnavailableError):
            async with adapter.hear("de"):
                pytest.fail("missing private socket yielded a hearing")

    asyncio.run(refused())


class _CallerFaultError(Exception):
    pass


async def _raise_caller_fault(adapter: UdsCoPresenter) -> None:
    async with adapter.hear("caller-fault") as hearing:
        assert await hearing.receive() == HearingTranscript(text="opened", final=True)
        raise _CallerFaultError


def test_private_status_and_malformed_protocol_map_to_typed_unavailability(
    tmp_path: Path,
) -> None:
    asyncio.run(_refuse_private_failures(tmp_path))


async def _refuse_private_failures(tmp_path: Path) -> None:
    app = FastAPI()

    async def _who() -> JSONResponse:
        return JSONResponse({"private": "detail"}, status_code=503)

    async def _ask(request: Request) -> StreamingResponse:
        question = await request.json()

        async def events() -> AsyncIterator[bytes]:
            if question["said"] == "Unbekannt":
                yield b'event: private-metadata\ndata: {"text":"not public"}\n\n'
                return
            yield b'event: error\ndata: {"private":"detail"}\n\n'
            yield b'event: audio\ndata: {"text":"Antwort","wav_b64":"not base64"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    async def _hear(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"text": ["not", "text"], "final": True})
        await socket.close()

    app.add_api_route("/who", _who, methods=["GET"])
    app.add_api_route("/ask", _ask, methods=["POST"])
    app.add_api_websocket_route("/hear", _hear)
    socket_path = tmp_path / "copresenter.sock"
    adapter = UdsCoPresenter(socket_path)

    async with _Serving(app, socket_path):
        with pytest.raises(CoPresenterUnavailableError):
            await adapter.readiness()
        with pytest.raises(CoPresenterUnavailableError):
            await _read_error_then_malformed_audio(adapter)
        with pytest.raises(CoPresenterUnavailableError):
            await _read_unknown_private_event(adapter)
        with pytest.raises(CoPresenterUnavailableError):
            async with adapter.hear("de") as hearing:
                await hearing.receive()


async def _read_error_then_malformed_audio(adapter: UdsCoPresenter) -> None:
    async with adapter.answer(Question(said="Frage", slide=1, language=None)) as answer:
        assert await anext(answer) == AnswerUnavailable()
        await anext(answer)


async def _read_unknown_private_event(adapter: UdsCoPresenter) -> None:
    async with adapter.answer(
        Question(said="Unbekannt", slide=1, language=None)
    ) as answer:
        await anext(answer)


def test_cancelled_native_answer_closes_the_private_stream(tmp_path: Path) -> None:
    asyncio.run(_cancel_native_answer(tmp_path))


async def _cancel_native_answer(tmp_path: Path) -> None:
    app = FastAPI()
    stream_closed = asyncio.Event()

    async def _ask() -> StreamingResponse:
        async def events() -> AsyncIterator[bytes]:
            try:
                yield b'event: text\ndata: {"text":"Antwort"}\n\n'
                await asyncio.Future()
            finally:
                stream_closed.set()

        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_api_route("/ask", _ask, methods=["POST"])
    socket_path = tmp_path / "copresenter.sock"

    async with _Serving(app, socket_path):
        async with UdsCoPresenter(socket_path).answer(
            Question(said="Frage", slide=1, language=None)
        ) as answer:
            assert await anext(answer) == Text(text="Antwort")
        await asyncio.wait_for(stream_closed.wait(), timeout=1)


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
        await self._server.shutdown(sockets=[self._listener])
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
