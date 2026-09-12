"""The speech adapter uses its UDS and reports one expected unavailable state."""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Self

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from httpcore2._backends.auto import AutoBackend

from presentator.adapters import speech
from presentator.adapters.speech import UdsSpeech
from presentator.contracts.voice import (
    SampleLanguage,
    VoiceId,
    VoiceLoadOutcome,
    VoiceSampleBusyError,
    VoiceUnavailableError,
)


def test_missing_private_socket_never_falls_back_to_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted_tcp: list[dict[str, object]] = []

    async def refuse_tcp(_backend: AutoBackend, **connection: object) -> None:
        attempted_tcp.append(connection)
        raise AssertionError

    monkeypatch.setattr(AutoBackend, "connect_tcp", refuse_tcp)
    reader = UdsSpeech(tmp_path / "missing.sock")

    async def read() -> None:
        with pytest.raises(VoiceUnavailableError):
            await reader.snapshot()

    asyncio.run(read())

    assert attempted_tcp == []


def test_uds_adapter_uses_the_private_voice_path_and_validates_the_snapshot(
    tmp_path: Path,
) -> None:
    asyncio.run(_read_voice_status(tmp_path))


async def _read_voice_status(tmp_path: Path) -> None:
    app = FastAPI()

    async def voices(request: Request) -> dict[str, object]:
        assert request.url.path == "/voices"
        return {
            "voices": [
                {
                    "id": "piper",
                    "name": "Piper",
                    "language": "German — Thorsten voice",
                    "state": "active",
                }
            ]
        }

    app.add_api_route("/voices", voices, methods=["GET"])
    socket_path = tmp_path / "speech.sock"
    async with _Serving(app, socket_path):
        snapshot = await UdsSpeech(socket_path).snapshot()

    assert snapshot.voices[0].name == "Piper"


@pytest.mark.parametrize(
    "outcome",
    [
        VoiceLoadOutcome.ACTIVATED,
        VoiceLoadOutcome.ACTIVATED_DURABILITY_UNCONFIRMED,
        VoiceLoadOutcome.NOT_ACTIVATED,
    ],
)
def test_uds_adapter_posts_the_closed_voice_id_and_returns_the_typed_outcome(
    tmp_path: Path, outcome: VoiceLoadOutcome
) -> None:
    asyncio.run(_load_voice(tmp_path, outcome))


async def _load_voice(tmp_path: Path, expected: VoiceLoadOutcome) -> None:
    app = FastAPI()

    async def load(request: Request) -> dict[str, str]:
        assert request.url.path == "/voices/chatterbox/load"
        return {"outcome": expected.value}

    app.add_api_route("/voices/chatterbox/load", load, methods=["POST"])
    socket_path = tmp_path / "speech.sock"
    async with _Serving(app, socket_path):
        outcome = await UdsSpeech(socket_path).load(VoiceId.CHATTERBOX)

    assert outcome is expected


def test_uds_adapter_returns_only_nonempty_wav_from_the_closed_sample_path(
    tmp_path: Path,
) -> None:
    asyncio.run(_sample_voice(tmp_path))


async def _sample_voice(tmp_path: Path) -> None:
    app = FastAPI()

    async def sample(request: Request) -> Response:
        assert request.method == "POST"
        assert request.url.path == "/voices/piper/sample/en"
        return Response(content=b"wav", media_type="audio/wav")

    app.add_api_route("/voices/piper/sample/en", sample, methods=["POST"])
    socket_path = tmp_path / "speech.sock"
    async with _Serving(app, socket_path):
        audio = await UdsSpeech(socket_path).sample(
            VoiceId.PIPER, SampleLanguage.ENGLISH
        )

    assert audio == b"wav"


def test_uds_adapter_maps_private_sample_contention_to_the_typed_busy_result(
    tmp_path: Path,
) -> None:
    asyncio.run(_sample_busy(tmp_path))


async def _sample_busy(tmp_path: Path) -> None:
    app = FastAPI()
    app.add_api_route(
        "/voices/piper/sample/de",
        lambda: Response(status_code=409),
        methods=["POST"],
    )
    socket_path = tmp_path / "speech.sock"
    async with _Serving(app, socket_path):
        with pytest.raises(VoiceSampleBusyError):
            await UdsSpeech(socket_path).sample(VoiceId.PIPER, SampleLanguage.GERMAN)


def test_missing_private_socket_refuses_a_sample_without_tcp_fallback(
    tmp_path: Path,
) -> None:
    async def sample() -> None:
        with pytest.raises(VoiceUnavailableError):
            await UdsSpeech(tmp_path / "missing.sock").sample(
                VoiceId.PIPER, SampleLanguage.GERMAN
            )

    asyncio.run(sample())


@pytest.mark.parametrize(
    ("status", "headers", "content"),
    [
        (200, {"content-type": "text/plain"}, b"wav"),
        (200, {"content-type": "audio/wav"}, b""),
        (503, {}, b""),
    ],
)
def test_uds_sample_maps_invalid_media_and_nonbusy_failures_to_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    content: bytes,
) -> None:
    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_error: object) -> None:
            return None

        async def post(self, path: str) -> httpx2.Response:
            return httpx2.Response(
                status,
                headers=headers,
                content=content,
                request=httpx2.Request("POST", f"http://speech.localhost{path}"),
            )

    def transport(**_arguments: object) -> object:
        return object()

    def client(**_arguments: object) -> Client:
        return Client()

    monkeypatch.setattr(speech.httpx2, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(speech.httpx2, "AsyncClient", client)

    async def sample() -> None:
        with pytest.raises(VoiceUnavailableError):
            await UdsSpeech(tmp_path / "missing.sock").sample(
                VoiceId.PIPER, SampleLanguage.GERMAN
            )

    asyncio.run(sample())


def test_uds_sample_cancellation_closes_its_private_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    closed = False

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_error: object) -> None:
            nonlocal closed
            closed = True

        async def post(self, _path: str) -> httpx2.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError

    def transport(**_arguments: object) -> object:
        return object()

    def client(**_arguments: object) -> Client:
        return Client()

    monkeypatch.setattr(speech.httpx2, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(speech.httpx2, "AsyncClient", client)

    async def cancel() -> None:
        task = asyncio.create_task(
            UdsSpeech(tmp_path / "speech.sock").sample(
                VoiceId.PIPER, SampleLanguage.GERMAN
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert closed


async def _read_status(reader: UdsSpeech) -> object:
    return await reader.snapshot()


async def _request_voice_load(reader: UdsSpeech) -> object:
    return await reader.load(VoiceId.PIPER)


def _failure_body(*, operation: str, scenario: str) -> object:
    if scenario != "unknown":
        return {"voices": [dict[str, object]()]}
    if operation == "status":
        return {
            "voices": [
                {
                    "id": "unknown",
                    "name": "Unknown",
                    "language": "Unknown",
                    "state": "active",
                }
            ]
        }
    return {"outcome": "unknown"}


@pytest.mark.parametrize(
    ("operation", "reader_action"),
    [("status", _read_status), ("load", _request_voice_load)],
)
@pytest.mark.parametrize("scenario", ["malformed", "unknown", "timed_out"])
def test_uds_adapter_maps_private_response_failures_to_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    reader_action: Callable[[UdsSpeech], Awaitable[object]],
    scenario: str,
) -> None:
    body = _failure_body(operation=operation, scenario=scenario)
    failure = (
        httpx2.ReadTimeout("private speech timed out")
        if scenario == "timed_out"
        else None
    )

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_error: object) -> None:
            return None

        async def _response(self, method: str, path: str) -> httpx2.Response:
            if failure is not None:
                raise failure
            return httpx2.Response(
                200,
                json=body,
                request=httpx2.Request(method, f"http://speech.localhost{path}"),
            )

        async def get(self, path: str) -> httpx2.Response:
            return await self._response("GET", path)

        async def post(self, _path: str) -> httpx2.Response:
            return await self._response("POST", "/voices/piper/load")

    def transport(**_arguments: object) -> object:
        return object()

    def client(**_arguments: object) -> Client:
        return Client()

    monkeypatch.setattr(speech.httpx2, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(speech.httpx2, "AsyncClient", client)
    reader = UdsSpeech(tmp_path / "speech.sock")

    async def verify() -> None:
        with pytest.raises(VoiceUnavailableError):
            await reader_action(reader)

    asyncio.run(verify())


class _Serving:
    """One owned temporary UDS peer that stops with its test."""

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
