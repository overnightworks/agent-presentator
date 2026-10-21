"""Hearing and its readiness over speech's private socket, against a fake of speech."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from httpcore2._backends.auto import AutoBackend

from presentator.adapters.copresenter import UdsCoPresenter
from presentator.adapters.speech import UdsSpeechHearing
from presentator.application.copresenter import CoPresenterUse
from presentator.contracts.copresenter import (
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    HearingTranscript,
)
from tests.adapters.serving import Serving, unix_listener

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

type Hear = Callable[[WebSocket], Awaitable[None]]


def speech_health(*, ready: bool = True) -> dict[str, object]:
    """The body speech's /health answers; the speaking rate differs from hearing's."""
    return {
        "speaking": {"model": "voice", "ready": True, "sample_rate": 22_050},
        "hearing": {"model": "whisper", "ready": ready},
        "sample_rate": 16_000,
        "card_memory_mb": 1_200,
    }


def speech_app(
    *, health: Callable[[], JSONResponse] | None = None, hear: Hear | None = None
) -> FastAPI:
    app = FastAPI()

    async def _health() -> JSONResponse:
        return health() if health else JSONResponse(speech_health())

    app.add_api_route("/health", _health, methods=["GET"])
    if hear is not None:
        app.add_api_websocket_route("/hear", hear)
    return app


def test_readiness_is_speechs_hearing_rate_and_state_without_credentials(
    tmp_path: Path,
) -> None:
    seen_headers: dict[str, str] = {}
    seen_paths: list[str] = []
    app = FastAPI()

    async def _health(request: Request) -> JSONResponse:
        seen_headers.update(request.headers)
        seen_paths.append(request.url.path)
        return JSONResponse(speech_health())

    app.add_api_route("/health", _health, methods=["GET"])

    readiness = asyncio.run(_readiness(tmp_path, app))

    assert readiness == CoPresenterReadiness(
        hearing_sample_rate=16_000, local_hearing_ready=True
    )
    assert seen_paths == ["/health"]
    assert not {"cookie", "authorization", "x-csrf-token"} & seen_headers.keys()


async def _readiness(tmp_path: Path, app: FastAPI) -> CoPresenterReadiness:
    socket_path = tmp_path / "speech.sock"
    async with Serving(app, unix_listener(socket_path)):
        return await UdsSpeechHearing(socket_path).readiness()


def test_hearing_that_is_still_loading_is_not_ready(tmp_path: Path) -> None:
    app = speech_app(health=lambda: JSONResponse(speech_health(ready=False)))

    readiness = asyncio.run(_readiness(tmp_path, app))

    assert readiness.local_hearing_ready is False


def _health_with(**changes: object) -> Callable[[], JSONResponse]:
    return lambda: JSONResponse({**speech_health(), **changes})


@pytest.mark.parametrize(
    "health",
    [
        pytest.param(
            lambda: JSONResponse({"detail": "private"}, status_code=503), id="refused"
        ),
        pytest.param(
            lambda: JSONResponse({"sample_rate": 16_000}), id="no-hearing-section"
        ),
        pytest.param(_health_with(sample_rate="16000"), id="rate-as-text"),
        pytest.param(_health_with(hearing={"ready": "true"}), id="ready-as-text"),
        pytest.param(_health_with(hearing={"ready": 1}), id="ready-as-number"),
    ],
)
def test_a_refused_or_malformed_health_is_unavailable(
    tmp_path: Path, health: Callable[[], JSONResponse]
) -> None:
    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(_readiness(tmp_path, speech_app(health=health)))


def test_a_missing_socket_is_unavailable_without_a_tcp_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted_tcp: list[dict[str, object]] = []

    async def refuse_tcp(_backend: AutoBackend, **connection: object) -> None:
        attempted_tcp.append(connection)
        raise AssertionError

    monkeypatch.setattr(AutoBackend, "connect_tcp", refuse_tcp)

    asyncio.run(_refuse_missing_socket(tmp_path))

    assert attempted_tcp == []


async def _refuse_missing_socket(tmp_path: Path) -> None:
    speech = UdsSpeechHearing(tmp_path / "missing.sock")
    with pytest.raises(CoPresenterUnavailableError):
        await speech.readiness()
    with pytest.raises(CoPresenterUnavailableError):
        async with speech.hear("de"):
            pytest.fail("missing speech socket yielded a hearing")


def test_hearing_passes_pcm_up_and_transcripts_down(tmp_path: Path) -> None:
    received: list[bytes] = []
    queries: list[dict[str, str]] = []

    async def _hear(socket: WebSocket) -> None:
        queries.append(dict(socket.query_params))
        await socket.accept()
        received.append(await socket.receive_bytes())
        await socket.send_json({"text": "geh", "final": False})
        await socket.send_json({"text": "gehört", "final": True})

    async def _use() -> list[HearingTranscript | None]:
        socket_path = tmp_path / "speech.sock"
        async with (
            Serving(speech_app(hear=_hear), unix_listener(socket_path)),
            UdsSpeechHearing(socket_path).hear("de") as hearing,
        ):
            await hearing.send_pcm(b"pcm")
            return [await hearing.receive(), await hearing.receive()]

    transcripts = asyncio.run(_use())

    assert received == [b"pcm"]
    assert queries == [{"language": "de"}]
    assert transcripts == [
        HearingTranscript(text="geh", final=False),
        HearingTranscript(text="gehört", final=True),
    ]


@pytest.mark.parametrize(
    ("code", "after_frame"),
    [(1013, False), (1009, True), (1011, False), (1000, False)],
)
def test_every_close_speech_sends_ends_the_hearing_quietly(
    tmp_path: Path, code: int, *, after_frame: bool
) -> None:
    async def _hear(socket: WebSocket) -> None:
        await socket.accept()
        if after_frame:
            await socket.receive_bytes()
        await socket.close(code=code)

    async def _use() -> HearingTranscript | None:
        socket_path = tmp_path / "speech.sock"
        async with (
            Serving(speech_app(hear=_hear), unix_listener(socket_path)),
            UdsSpeechHearing(socket_path).hear("de") as hearing,
        ):
            await hearing.send_pcm(b"pcm")
            return await hearing.receive()

    assert asyncio.run(_use()) is None


class _CallerFaultError(Exception):
    pass


def test_a_malformed_transcript_is_unavailable(tmp_path: Path) -> None:
    async def _hear(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"text": ["not", "text"], "final": True})
        await socket.close()

    async def _use() -> None:
        socket_path = tmp_path / "speech.sock"
        async with (
            Serving(speech_app(hear=_hear), unix_listener(socket_path)),
            UdsSpeechHearing(socket_path).hear("de") as hearing,
        ):
            await hearing.receive()

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(_use())


def test_a_peer_that_crashes_during_send_is_unavailable(tmp_path: Path) -> None:
    async def _hear(socket: WebSocket) -> None:
        await socket.accept()
        message = "speech crashed"
        raise RuntimeError(message)

    async def _use() -> None:
        socket_path = tmp_path / "speech.sock"
        async with (
            Serving(speech_app(hear=_hear), unix_listener(socket_path)),
            UdsSpeechHearing(socket_path).hear("de") as hearing,
        ):
            await hearing.send_pcm(b"pcm")

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(_use())


def test_a_fault_of_the_caller_is_not_mapped(tmp_path: Path) -> None:
    async def _hear(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"text": "opened", "final": True})
        await socket.close()

    async def _use() -> None:
        socket_path = tmp_path / "speech.sock"
        async with (
            Serving(speech_app(hear=_hear), unix_listener(socket_path)),
            UdsSpeechHearing(socket_path).hear("de") as hearing,
        ):
            assert await hearing.receive() == HearingTranscript(
                text="opened", final=True
            )
            raise _CallerFaultError

    with pytest.raises(ExceptionGroup) as propagated:
        asyncio.run(_use())

    assert propagated.value.subgroup(_CallerFaultError) is not None


def test_ending_the_hearing_closes_speechs_side(tmp_path: Path) -> None:
    async def _use() -> str:
        peer_saw: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def _hear(socket: WebSocket) -> None:
            await socket.accept()
            message = await socket.receive()
            peer_saw.set_result(message["type"])

        socket_path = tmp_path / "speech.sock"
        use = CoPresenterUse(
            answers=UdsCoPresenter(tmp_path / "unused.sock"),
            speech=UdsSpeechHearing(socket_path),
        )
        async with Serving(speech_app(hear=_hear), unix_listener(socket_path)):
            async with use.hear("de"):
                pass
            return await asyncio.wait_for(peer_saw, timeout=1)

    assert asyncio.run(_use()) == "websocket.disconnect"
