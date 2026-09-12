"""Read speech status and synchronously Load a voice through its private Unix socket."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx2
from pydantic import BaseModel, ValidationError

from presentator.contracts.voice import (
    SampleLanguage,
    VoiceId,
    VoiceLoadOutcome,
    VoiceRecovery,
    VoiceRecoveryKind,
    VoiceSampleBusyError,
    VoiceSnapshot,
    VoiceState,
    VoiceStatus,
    VoiceUnavailableError,
)

if TYPE_CHECKING:
    from pathlib import Path

_PRIVATE_ORIGIN: Final = "http://speech.localhost"
_STATUS_TIMEOUT: Final = 5.0
_BUSY_STATUS: Final = 409


class _VoicePayload(BaseModel):
    id: VoiceId
    name: str
    language: str
    state: VoiceState


class _RecoveryPayload(BaseModel):
    kind: VoiceRecoveryKind
    voice: VoiceId | None = None


class _StatusPayload(BaseModel):
    voices: tuple[_VoicePayload, ...]
    recovery: _RecoveryPayload | None = None


class _LoadPayload(BaseModel):
    outcome: VoiceLoadOutcome


class UdsSpeech:
    """Use the private speech status and Load protocol at its configured UDS."""

    def __init__(self, socket_path: Path) -> None:
        """Keep the one configured speech socket path."""
        self._socket_path = socket_path

    async def snapshot(self) -> VoiceSnapshot:
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
        return VoiceSnapshot(
            voices=tuple(
                VoiceStatus(
                    id=row.id,
                    name=row.name,
                    language=row.language,
                    state=row.state,
                )
                for row in payload.voices
            ),
            recovery=(
                None
                if payload.recovery is None
                else VoiceRecovery(
                    kind=payload.recovery.kind,
                    voice=payload.recovery.voice,
                )
            ),
        )

    async def load(self, voice: VoiceId) -> VoiceLoadOutcome:
        """Submit one bounded private mutation and validate its typed outcome."""
        transport = httpx2.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx2.AsyncClient(
                base_url=_PRIVATE_ORIGIN,
                transport=transport,
                timeout=httpx2.Timeout(connect=5.0, write=5.0, pool=5.0, read=None),
                trust_env=False,
            ) as client:
                response = await client.post(f"/voices/{voice.value}/load")
                response.raise_for_status()
                payload = _LoadPayload.model_validate(response.json())
        except (httpx2.HTTPError, ValidationError, TypeError, ValueError) as refused:
            raise VoiceUnavailableError from refused
        return payload.outcome

    async def sample(self, voice: VoiceId, language: SampleLanguage) -> bytes:
        """Buffer one bounded private WAV and reject malformed media at the boundary."""
        transport = httpx2.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx2.AsyncClient(
                base_url=_PRIVATE_ORIGIN,
                transport=transport,
                timeout=httpx2.Timeout(connect=5.0, write=5.0, pool=5.0, read=None),
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"/voices/{voice.value}/sample/{language.value}"
                )
                if response.status_code == _BUSY_STATUS:
                    _raise_sample_busy()
                response.raise_for_status()
                audio = response.content
                if response.headers.get("content-type") != "audio/wav" or not audio:
                    _raise_invalid_audio()
        except VoiceSampleBusyError:
            raise
        except (httpx2.HTTPError, TypeError, ValueError) as refused:
            raise VoiceUnavailableError from refused
        return audio


def _raise_sample_busy() -> None:
    raise VoiceSampleBusyError


def _raise_invalid_audio() -> None:
    raise ValueError
