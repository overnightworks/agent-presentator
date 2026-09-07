"""The one boundary that speaks and hears: the local speech service by default."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx
import websockets
from websockets import ClientConnection

_HEALTH_TIMEOUT = 2.0
_SPEAK_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class SpeechModel:
    """One half of the speech service, as `/health` reports it."""

    model: str
    ready: bool


@dataclass(frozen=True, slots=True)
class SpeechHealth:
    """What `GET /health` on the speech service answered."""

    speaking: SpeechModel
    hearing: SpeechModel
    sample_rate: int
    card_memory_mb: int
    reachable: bool


class Speech(Protocol):
    """Speak text and open a hearing socket. Never loads a model."""

    async def health(self) -> SpeechHealth:
        """Return `/health`, or an unreachable record."""
        ...

    async def speak(self, text: str, language: str) -> bytes:
        """Return one sentence as a WAV body."""
        ...

    def hear_url(self, language: str) -> str:
        """The hearing socket for this language."""
        ...

    async def open_hear(self, language: str) -> ClientConnection:
        """Open that socket. The caller owns the connection."""
        ...


def _ws_base(http_url: str) -> str:
    if http_url.startswith("https://"):
        return f"wss://{http_url.removeprefix('https://')}"
    return f"ws://{http_url.removeprefix('http://')}"


class LocalSpeech:
    """HTTP client for the local speech service's fixed contract."""

    def __init__(self, address: str, *, client: httpx.AsyncClient | None = None) -> None:
        """Point at a speech service. Pass a client to share one in tests."""
        self._address = address.rstrip("/")
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the HTTP client this instance created."""
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> SpeechHealth:
        """Read `/health`. Unreachable is a structured miss, not an exception."""
        try:
            response = await self._client.get(
                f"{self._address}/health",
                timeout=_HEALTH_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return _unreachable()
        return _health_from_payload(payload)

    async def speak(self, text: str, language: str) -> bytes:
        """POST one sentence to `/speak` and return the chunked WAV body."""
        async with self._client.stream(
            "POST",
            f"{self._address}/speak",
            json={"text": text, "language": language},
            timeout=_SPEAK_TIMEOUT,
        ) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
            return bytes(chunks)

    def hear_url(self, language: str) -> str:
        """The speech service's hearing socket for this language."""
        return f"{_ws_base(self._address)}/hear?language={language}"

    async def open_hear(self, language: str) -> ClientConnection:
        """Open the hearing socket. The caller owns the connection."""
        return await websockets.connect(self.hear_url(language))


def _unreachable() -> SpeechHealth:
    missing = SpeechModel(model="unreachable", ready=False)
    return SpeechHealth(
        speaking=missing,
        hearing=missing,
        sample_rate=16000,
        card_memory_mb=0,
        reachable=False,
    )


def _health_from_payload(payload: object) -> SpeechHealth:
    if not isinstance(payload, dict):
        return _unreachable()
    speaking = _model(payload.get("speaking"))
    hearing = _model(payload.get("hearing"))
    sample_rate = payload.get("sample_rate", 16000)
    card_memory = payload.get("card_memory_mb", 0)
    if not isinstance(sample_rate, int) or not isinstance(card_memory, int):
        return _unreachable()
    return SpeechHealth(
        speaking=speaking,
        hearing=hearing,
        sample_rate=sample_rate,
        card_memory_mb=card_memory,
        reachable=True,
    )


def _model(value: object) -> SpeechModel:
    if not isinstance(value, dict):
        return SpeechModel(model="unknown", ready=False)
    name = value.get("model", "unknown")
    ready = value.get("ready", False)
    if not isinstance(name, str) or not isinstance(ready, bool):
        return SpeechModel(model="unknown", ready=False)
    return SpeechModel(model=name, ready=ready)
