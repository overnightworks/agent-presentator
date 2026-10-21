"""The adapter speaks the maintained SSE and WebSocket protocols over one UDS."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpcore2._backends.auto import AutoBackend

from presentator.adapters.copresenter import UdsCoPresenter
from presentator.contracts.copresenter import (
    AnswerText,
    AnswerUnavailable,
    Audio,
    CoPresenterUnavailableError,
    Done,
    Question,
    Sentence,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
from tests.adapters.serving import Serving, unix_listener


def test_uds_adapter_maps_native_incremental_sse(tmp_path: Path) -> None:
    asyncio.run(_read_answer(tmp_path))


async def _read_answer(tmp_path: Path) -> None:
    seen_headers: dict[str, str] = {}
    app = FastAPI()

    async def _ask(request: Request) -> StreamingResponse:
        seen_headers.update(request.headers)
        assert await request.json() == {"said": "Frage", "slide": 1, "language": "de"}

        async def events() -> AsyncIterator[bytes]:
            yield b'event: text\ndata: {"text":\ndata: "Antwort"}\n\n'
            yield b'event: sentence\ndata: {"text":"Antwort."}\n\n'
            yield b'event: audio\ndata: {"text":"Antwort","wav_b64":"UklGRg=="}\n\n'
            yield b'event: done\ndata: {"text":"Antwort"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_api_route("/ask", _ask, methods=["POST"])

    socket_path = tmp_path / "copresenter.sock"
    async with Serving(app, unix_listener(socket_path)):
        adapter = UdsCoPresenter(socket_path)
        async with adapter.answer(
            Question(said="Frage", slide=1, language="de")
        ) as answer:
            events = [event async for event in answer]

    assert events == [
        AnswerText(text="Antwort"),
        Sentence(text="Antwort."),
        Audio(text="Antwort", wav=b"RIFF"),
        Done(text="Antwort"),
    ]
    assert "cookie" not in seen_headers
    assert "authorization" not in seen_headers
    assert "x-csrf-token" not in seen_headers


def test_missing_socket_maps_to_typed_unavailability_without_tcp_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_tcp: list[dict[str, object]] = []

    async def refuse_tcp(
        _backend: AutoBackend,
        **connection: object,
    ) -> None:
        attempted_tcp.append(connection)
        raise AssertionError

    monkeypatch.setattr(AutoBackend, "connect_tcp", refuse_tcp)
    asyncio.run(_refuse_missing_socket(tmp_path))

    assert attempted_tcp == []


async def _refuse_missing_socket(tmp_path: Path) -> None:
    adapter = UdsCoPresenter(tmp_path / "missing.sock")
    with pytest.raises(CoPresenterUnavailableError):
        await _read_missing_answer(adapter)


async def _read_missing_answer(adapter: UdsCoPresenter) -> None:
    async with adapter.answer(Question(said="Frage", slide=1, language=None)) as answer:
        await anext(answer)


def test_private_status_and_malformed_protocol_map_to_typed_unavailability(
    tmp_path: Path,
) -> None:
    asyncio.run(_refuse_private_failures(tmp_path))


async def _refuse_private_failures(tmp_path: Path) -> None:
    app = FastAPI()

    async def _ask(request: Request) -> StreamingResponse:
        question = await request.json()

        async def events() -> AsyncIterator[bytes]:
            if question["said"] == "Unbekannt":
                yield b'event: private-metadata\ndata: {"text":"not public"}\n\n'
                return
            yield b'event: error\ndata: {"private":"detail"}\n\n'
            yield b'event: audio\ndata: {"text":"Antwort","wav_b64":"not base64"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_api_route("/ask", _ask, methods=["POST"])
    socket_path = tmp_path / "copresenter.sock"
    adapter = UdsCoPresenter(socket_path)

    async with Serving(app, unix_listener(socket_path)):
        with pytest.raises(CoPresenterUnavailableError):
            await _read_error_then_malformed_audio(adapter)
        with pytest.raises(CoPresenterUnavailableError):
            await _read_unknown_private_event(adapter)


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

    async with Serving(app, unix_listener(socket_path)):
        async with UdsCoPresenter(socket_path).answer(
            Question(said="Frage", slide=1, language=None)
        ) as answer:
            assert await anext(answer) == AnswerText(text="Antwort")
        await asyncio.wait_for(stream_closed.wait(), timeout=1)
