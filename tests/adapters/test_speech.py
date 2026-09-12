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
from fastapi import FastAPI, Request
from httpcore2._backends.auto import AutoBackend

from presentator.adapters import speech
from presentator.adapters.speech import UdsSpeech
from presentator.contracts.voice import VoiceId, VoiceLoadOutcome, VoiceUnavailableError


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
