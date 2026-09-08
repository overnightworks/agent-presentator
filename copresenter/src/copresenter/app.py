"""HTTP surface: who is answering, ask with streamed speech, hear proxy."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from websockets.exceptions import WebSocketException

from copresenter.answer import Answerer, stream_spoken
from copresenter.config import Settings
from copresenter.deck import Deck, load_deck
from copresenter.speech import LocalSpeech, Speech, SpeechHealth

_log = logging.getLogger("copresenter")
_POLICY_VIOLATION = 1008


class AskRequest(BaseModel):
    """What the person said, and which slide they are on."""

    said: str = Field(min_length=1)
    slide: int = Field(ge=1)
    language: str | None = None


def create_app(
    settings: Settings,
    *,
    deck: Deck,
    speech: Speech,
    answerer: Answerer,
) -> FastAPI:
    """Compose the service. Tests inject a canned answerer and a fake speech port."""
    app = FastAPI(title="copresenter")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.allowed_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/who")
    async def who() -> dict[str, object]:
        """Name the answering model and the speech models behind it."""
        health = await speech.health()
        return {
            "answerer": {"provider": answerer.provider, "model": answerer.model},
            "speech": _speech_report(health, settings.speech_url),
            "deck": {"title": deck.title, "slides": len(deck.slides), "path": str(deck.source)},
        }

    @app.post("/ask")
    async def ask(request: AskRequest) -> StreamingResponse:
        """Stream the model's text, then each sentence as soon as it can be spoken."""
        language = request.language or settings.language
        return StreamingResponse(
            _ask_events(
                said=request.said,
                slide=request.slide,
                language=language,
                deck=deck,
                answerer=answerer,
                speech=speech,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/hear")
    async def hear(socket: WebSocket, language: str = "de") -> None:
        """Forward raw PCM to the speech service and JSON transcripts back."""
        if _is_foreign_origin(socket, settings.allowed_origin):
            await socket.close(code=_POLICY_VIOLATION)
            return
        await socket.accept()
        try:
            upstream = await speech.open_hear(language)
        except (OSError, WebSocketException, TimeoutError) as exc:
            _log.warning("hearing socket could not open: %s", type(exc).__name__)
            await socket.send_json({"text": "", "final": True, "error": "hearing unavailable"})
            await socket.close()
            return
        await _pipe_hear(socket, upstream)

    return app


async def _ask_events(
    *,
    said: str,
    slide: int,
    language: str,
    deck: Deck,
    answerer: Answerer,
    speech: Speech,
) -> AsyncIterator[bytes]:
    try:
        async for kind, payload in stream_spoken(
            said=said,
            slide=slide,
            deck=deck,
            answerer=answerer,
            speech=speech,
            language=language,
        ):
            yield _sse(kind, payload)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, KeyError):
        _log.exception("ask failed")
        yield _sse("error", {"message": "the answer could not be spoken"})


def _sse(kind: str, payload: dict[str, object]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode()


def _speech_report(health: SpeechHealth, address: str) -> dict[str, object]:
    return {
        "address": address,
        "reachable": health.reachable,
        "sample_rate": health.sample_rate,
        "card_memory_mb": health.card_memory_mb,
        "speaking": {"model": health.speaking.model, "ready": health.speaking.ready},
        "hearing": {"model": health.hearing.model, "ready": health.hearing.ready},
    }


def _is_foreign_origin(socket: WebSocket, allowed: str) -> bool:
    """Cross-origin middleware does not see sockets; a browser names its page here."""
    origin = socket.headers.get("origin")
    return origin is not None and origin != allowed


async def _pipe_hear(socket: WebSocket, upstream: object) -> None:
    """Copy PCM up and transcripts down until either side ends, then stop both."""
    upward = asyncio.create_task(_forward_up(socket, upstream))
    downward = asyncio.create_task(_forward_down(socket, upstream))
    pending: set[asyncio.Task[None]] = set()
    try:
        _done, pending = await asyncio.wait(
            {upward, downward},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in _done:
            _raise_if_failed(task)
        for task in pending:
            await _await_stopped(task)
    finally:
        await _close_quietly(upstream)
        await _close_quietly(socket)


async def _forward_up(socket: WebSocket, upstream: object) -> None:
    try:
        while True:
            message = await socket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data:
                await upstream.send(data)
    except WebSocketDisconnect:
        return


async def _forward_down(socket: WebSocket, upstream: object) -> None:
    try:
        async for message in upstream:
            if isinstance(message, bytes):
                await socket.send_bytes(message)
            else:
                await socket.send_text(message)
    except (WebSocketException, WebSocketDisconnect):
        return


def _raise_if_failed(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        raise exception


async def _await_stopped(task: asyncio.Task[None]) -> None:
    try:
        await task
    except (asyncio.CancelledError, WebSocketDisconnect, WebSocketException):
        return


async def _close_quietly(connection: object) -> None:
    close = getattr(connection, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except (WebSocketDisconnect, WebSocketException, RuntimeError, ConnectionError):
        return


def compose(settings: Settings, *, answerer: Answerer) -> FastAPI:
    """Production composition: local speech, the configured deck, this answerer."""
    deck = load_deck(settings.deck, language=settings.language)
    speech = LocalSpeech(settings.speech_url)
    return create_app(settings, deck=deck, speech=speech, answerer=answerer)
