"""The private co-presenter protocol over one Unix-domain socket."""

from __future__ import annotations

import binascii
from base64 import b64decode
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

import httpx2
from httpx2.websockets import (
    AsyncWebSocketSession,
    HTTPXWSException,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerUnavailable,
    Audio,
    CoPresenterReadiness,
    CoPresenterUnavailable,
    Done,
    HearingTranscript,
    HearingUnavailable,
    Question,
    Sentence,
    Text,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

    from presentator.ports.copresenter import PrivateHearing

_PRIVATE_ORIGIN: Final = "http://copresenter.internal"


class _Answerer(BaseModel):
    model: str


class _HearingReadiness(BaseModel):
    ready: bool


class _SpeechReadiness(BaseModel):
    sample_rate: int
    hearing: _HearingReadiness


class _Readiness(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answerer: _Answerer
    speech: _SpeechReadiness


class _TextPayload(BaseModel):
    text: str


class _AudioPayload(_TextPayload):
    wav_b64: str


class _TranscriptPayload(BaseModel):
    text: str
    final: bool
    error: str | None = None


class _UdsHearing:
    def __init__(self, socket: AsyncWebSocketSession) -> None:
        self._socket = socket

    async def send_pcm(self, frame: bytes) -> None:
        try:
            await self._socket.send_bytes(frame)
        except HTTPXWSException as refused:
            raise CoPresenterUnavailable from refused

    async def receive(self) -> HearingTranscript | HearingUnavailable | None:
        try:
            payload = _TranscriptPayload.model_validate(
                await self._socket.receive_json()
            )
        except WebSocketDisconnect:
            return None
        except (HTTPXWSException, ValidationError, TypeError, ValueError) as refused:
            raise CoPresenterUnavailable from refused
        if payload.error is not None:
            return HearingUnavailable()
        return HearingTranscript(text=payload.text, final=payload.final)

    async def aclose(self) -> None:
        try:
            await self._socket.close()
        except HTTPXWSException as refused:
            raise CoPresenterUnavailable from refused


class UdsCoPresenter:
    """Use only the typed private protocol at the configured UDS."""

    def __init__(self, socket_path: Path) -> None:
        """Keep the one configured private socket path."""
        self._socket_path = socket_path

    def _client(self, *, answer: bool = False) -> httpx2.AsyncClient:
        transport = httpx2.AsyncHTTPTransport(uds=str(self._socket_path))
        timeout = httpx2.Timeout(5.0, read=None) if answer else httpx2.Timeout(5.0)
        return httpx2.AsyncClient(
            base_url=_PRIVATE_ORIGIN,
            transport=transport,
            timeout=timeout,
            trust_env=False,
        )

    async def readiness(self) -> CoPresenterReadiness:
        """Validate and expose only the readiness fields the browser consumes."""
        try:
            async with self._client() as client:
                response = await client.get("/who")
                response.raise_for_status()
                private = _Readiness.model_validate(response.json())
        except (httpx2.HTTPError, ValidationError, TypeError, ValueError) as refused:
            raise CoPresenterUnavailable from refused
        return CoPresenterReadiness(
            answerer_model=private.answerer.model,
            hearing_sample_rate=private.speech.sample_rate,
            local_hearing_ready=private.speech.hearing.ready,
        )

    @asynccontextmanager
    async def answer(
        self, question: Question
    ) -> AsyncGenerator[AsyncIterator[AnswerEvent]]:
        """Stream one native SSE answer without buffering it."""
        client = self._client(answer=True)
        try:
            async with (
                client,
                client.sse(
                    "/ask",
                    method="POST",
                    json={
                        "said": question.said,
                        "slide": question.slide,
                        **(
                            {}
                            if question.language is None
                            else {"language": question.language}
                        ),
                    },
                ) as source,
            ):
                source.response.raise_for_status()
                yield self._answer_events(source)
        except (
            httpx2.HTTPError,
            ValidationError,
            TypeError,
            ValueError,
            binascii.Error,
        ) as refused:
            raise CoPresenterUnavailable from refused

    async def _answer_events(
        self, source: httpx2.EventSource
    ) -> AsyncIterator[AnswerEvent]:
        async for event in source:
            yield _answer_event(event)

    @asynccontextmanager
    async def hear(self, language: str) -> AsyncGenerator[PrivateHearing]:
        """Open one native private WebSocket hearing operation."""
        client = self._client()
        try:
            async with (
                client,
                client.websocket(
                    "ws://copresenter.internal/hear",
                    params={"language": language},
                ) as socket,
            ):
                yield _UdsHearing(socket)
        except (httpx2.HTTPError, HTTPXWSException, TypeError, ValueError) as refused:
            raise CoPresenterUnavailable from refused


def _answer_event(event: httpx2.ServerSentEvent) -> AnswerEvent:
    if event.event == "error":
        return AnswerUnavailable()
    if event.event == "audio":
        payload = _AudioPayload.model_validate_json(event.data)
        return Audio(text=payload.text, wav=b64decode(payload.wav_b64, validate=True))
    payload = _TextPayload.model_validate_json(event.data)
    if event.event == "text":
        return Text(text=payload.text)
    if event.event == "sentence":
        return Sentence(text=payload.text)
    if event.event == "done":
        return Done(text=payload.text)
    raise CoPresenterUnavailable
