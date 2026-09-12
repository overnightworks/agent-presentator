"""Read the speech status through its dedicated private Unix socket."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx2
from pydantic import BaseModel, ValidationError

from presentator.contracts.voice import (
    VoiceId,
    VoiceState,
    VoiceStatus,
    VoiceUnavailableError,
)

if TYPE_CHECKING:
    from pathlib import Path

_PRIVATE_ORIGIN: Final = "http://speech.localhost"
_STATUS_TIMEOUT: Final = 5.0


class _VoicePayload(BaseModel):
    id: VoiceId
    name: str
    language: str
    state: VoiceState


class _StatusPayload(BaseModel):
    voices: tuple[_VoicePayload, ...]


class UdsSpeech:
    """Use the status-only private speech protocol at its configured UDS."""

    def __init__(self, socket_path: Path) -> None:
        """Keep the one configured speech socket path."""
        self._socket_path = socket_path

    async def voices(self) -> tuple[VoiceStatus, ...]:
        """Validate the wire shape before it crosses the adapter boundary."""
        transport = httpx2.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx2.AsyncClient(
                base_url=_PRIVATE_ORIGIN,
                transport=transport,
                timeout=httpx2.Timeout(_STATUS_TIMEOUT),
                trust_env=False,
            ) as client:
                response = await client.get("/voices")
                response.raise_for_status()
                payload = _StatusPayload.model_validate(response.json())
        except (httpx2.HTTPError, ValidationError, TypeError, ValueError) as refused:
            raise VoiceUnavailableError from refused
        return tuple(
            VoiceStatus(
                id=row.id,
                name=row.name,
                language=row.language,
                state=row.state,
            )
            for row in payload.voices
        )
