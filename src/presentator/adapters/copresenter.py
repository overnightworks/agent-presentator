"""The private co-presenter answers over one Unix-domain socket."""

from __future__ import annotations

from base64 import b64decode
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

import httpx2
from pydantic import BaseModel, ValidationError

from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerText,
    AnswerUnavailable,
    Audio,
    CoPresenterUnavailableError,
    Done,
    Question,
    Sentence,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

_PRIVATE_ORIGIN: Final = "http://copresenter.localhost"


class _TextPayload(BaseModel):
    text: str


class _AudioPayload(_TextPayload):
    wav_b64: str


class UdsCoPresenter:
    """Use only the typed private protocol at the configured UDS."""

    def __init__(self, socket_path: Path) -> None:
        """Keep the one configured private socket path."""
        self._socket_path = socket_path

    def _client(self) -> httpx2.AsyncClient:
        transport = httpx2.AsyncHTTPTransport(uds=str(self._socket_path))
        return httpx2.AsyncClient(
            base_url=_PRIVATE_ORIGIN,
            transport=transport,
            timeout=httpx2.Timeout(5.0, read=None),
            trust_env=False,
        )

    @asynccontextmanager
    async def answer(
        self, question: Question
    ) -> AsyncGenerator[AsyncIterator[AnswerEvent]]:
        """Stream one native SSE answer without buffering it."""
        client = self._client()
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
        ) as refused:
            raise CoPresenterUnavailableError from refused

    async def _answer_events(
        self, source: httpx2.EventSource
    ) -> AsyncIterator[AnswerEvent]:
        async for event in source:
            yield _answer_event(event)


def _answer_event(event: httpx2.ServerSentEvent) -> AnswerEvent:
    if event.event == "error":
        return AnswerUnavailable()
    if event.event == "audio":
        payload = _AudioPayload.model_validate_json(event.data)
        return Audio(text=payload.text, wav=b64decode(payload.wav_b64, validate=True))
    payload = _TextPayload.model_validate_json(event.data)
    if event.event == "text":
        return AnswerText(text=payload.text)
    if event.event == "sentence":
        return Sentence(text=payload.text)
    if event.event == "done":
        return Done(text=payload.text)
    raise CoPresenterUnavailableError
