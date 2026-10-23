"""Read speech status, Load a voice and hear through its private Unix socket."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final, cast

import httpx2
from httpx2.websockets import (
    AsyncWebSocketSession,
    HTTPXWSException,
    WebSocketDisconnect,
)
from pydantic import BaseModel, StrictBool, StrictInt, ValidationError

from presentator.contracts.copresenter import (
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    HearingTranscript,
)
from presentator.contracts.voice import (
    SampleLanguage,
    VoiceDownloadOutcome,
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
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from presentator.ports.copresenter import PrivateHearing

_PRIVATE_ORIGIN: Final = "http://speech.localhost"
_STEP_TIMEOUT: Final = 5.0
_BUSY_STATUS: Final = 409
_SPEAK_READ_TIMEOUT: Final = 30.0


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


class _DownloadPayload(BaseModel):
    outcome: VoiceDownloadOutcome


class _HearingHealth(BaseModel):
    ready: StrictBool


class _Health(BaseModel):
    sample_rate: StrictInt
    hearing: _HearingHealth


class _TranscriptPayload(BaseModel):
    text: str
    final: bool


class _SpeechHearing:
    def __init__(self, socket: AsyncWebSocketSession) -> None:
        self._socket = socket

    async def send_pcm(self, frame: bytes) -> None:
        await self._socket.send_bytes(frame)

    async def receive(self) -> HearingTranscript | None:
        try:
            payload = _TranscriptPayload.model_validate(
                await self._socket.receive_json()
            )
        except WebSocketDisconnect:
            return None
        except (HTTPXWSException, ValidationError, TypeError, ValueError) as refused:
            raise CoPresenterUnavailableError from refused
        return HearingTranscript(text=payload.text, final=payload.final)

    async def aclose(self) -> None:
        await self._socket.close()


def _uds_client(socket_path: Path, timeout: httpx2.Timeout) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=_PRIVATE_ORIGIN,
        transport=httpx2.AsyncHTTPTransport(uds=str(socket_path)),
        timeout=timeout,
        trust_env=False,
    )


class UdsSpeaking:
    """Speak one sentence through speech's private socket."""

    def __init__(
        self, socket_path: Path, *, read_timeout: float = _SPEAK_READ_TIMEOUT
    ) -> None:
        """Keep the socket path and how long speech may stay silent mid-body."""
        self._socket_path = socket_path
        self._timeout = httpx2.Timeout(
            connect=_STEP_TIMEOUT,
            write=_STEP_TIMEOUT,
            pool=_STEP_TIMEOUT,
            read=read_timeout,
        )

    async def speak(self, text: str, language: str) -> bytes:
        """Read the whole WAV, or close the connection and raise."""
        try:
            async with (
                _uds_client(self._socket_path, self._timeout) as client,
                client.stream(
                    "POST", "/speak", json={"text": text, "language": language}
                ) as response,
            ):
                response.raise_for_status()
                audio = b"".join([chunk async for chunk in response.aiter_bytes()])
        except httpx2.HTTPError as refused:
            raise CoPresenterUnavailableError from refused
        if not audio:
            raise CoPresenterUnavailableError
        return audio


class UdsSpeechHearing:
    """Read speech's readiness and hear through its private socket."""

    def __init__(self, socket_path: Path) -> None:
        """Keep the one configured speech socket path."""
        self._socket_path = socket_path

    def _client(self) -> httpx2.AsyncClient:
        return _uds_client(self._socket_path, httpx2.Timeout(_STEP_TIMEOUT))

    async def readiness(self) -> CoPresenterReadiness:
        """Validate the two health fields the browser consumes."""
        try:
            async with self._client() as client:
                response = await client.get("/health")
                response.raise_for_status()
                health = _Health.model_validate(response.json())
        except (httpx2.HTTPError, ValidationError, TypeError, ValueError) as refused:
            raise CoPresenterUnavailableError from refused
        return CoPresenterReadiness(
            hearing_sample_rate=health.sample_rate,
            local_hearing_ready=health.hearing.ready,
        )

    @asynccontextmanager
    async def hear(self, language: str) -> AsyncGenerator[PrivateHearing]:
        """Open one hearing WebSocket on speech."""
        try:
            async with (
                self._client() as client,
                client.websocket("/hear", params={"language": language}) as socket,
            ):
                yield _SpeechHearing(socket)
        except BaseExceptionGroup as refused:
            if _is_expected_private_failure(refused):
                raise CoPresenterUnavailableError from refused
            raise
        except (httpx2.HTTPError, HTTPXWSException, TypeError, ValueError) as refused:
            raise CoPresenterUnavailableError from refused


def _is_expected_private_failure(refused: BaseException) -> bool:
    if isinstance(refused, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", refused)
        return all(_is_expected_private_failure(error) for error in group.exceptions)
    return isinstance(
        refused,
        (CoPresenterUnavailableError, httpx2.HTTPError, HTTPXWSException),
    )


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
                timeout=httpx2.Timeout(_STEP_TIMEOUT),
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
                timeout=httpx2.Timeout(
                    connect=_STEP_TIMEOUT,
                    write=_STEP_TIMEOUT,
                    pool=_STEP_TIMEOUT,
                    read=None,
                ),
                trust_env=False,
            ) as client:
                response = await client.post(f"/voices/{voice.value}/load")
                response.raise_for_status()
                payload = _LoadPayload.model_validate(response.json())
        except (httpx2.HTTPError, ValidationError, TypeError, ValueError) as refused:
            raise VoiceUnavailableError from refused
        return payload.outcome

    async def download_qwen(self) -> VoiceDownloadOutcome:
        """Start Qwen through one bounded private request."""
        transport = httpx2.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx2.AsyncClient(
                base_url=_PRIVATE_ORIGIN,
                transport=transport,
                timeout=httpx2.Timeout(_STEP_TIMEOUT),
                trust_env=False,
            ) as client:
                response = await client.post("/voices/qwen3-tts-0.6b/download")
                response.raise_for_status()
                payload = _DownloadPayload.model_validate(response.json())
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
                timeout=httpx2.Timeout(
                    connect=_STEP_TIMEOUT,
                    write=_STEP_TIMEOUT,
                    pool=_STEP_TIMEOUT,
                    read=None,
                ),
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
