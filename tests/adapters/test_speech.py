"""The speech adapter uses its UDS and reports one expected unavailable state."""

import asyncio
import socket
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
from presentator.contracts.voice import VoiceUnavailableError


def test_missing_private_socket_never_falls_back_to_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted_tcp: list[dict[str, object]] = []

    async def refuse_tcp(_backend: AutoBackend, **connection: object) -> None:
        attempted_tcp.append(connection)
        raise AssertionError

    monkeypatch.setattr(AutoBackend, "connect_tcp", refuse_tcp)

    async def read() -> None:
        with pytest.raises(VoiceUnavailableError):
            await UdsSpeech(tmp_path / "missing.sock").voices()

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
        rows = await UdsSpeech(socket_path).voices()

    assert rows[0].name == "Piper"


@pytest.mark.parametrize("scenario", ["malformed", "unknown", "timed_out"])
def test_uds_adapter_maps_malformed_unknown_or_timed_out_status_to_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    body: object = {"voices": [dict[str, object]()]}
    failure: httpx2.HTTPError | None = None
    if scenario == "unknown":
        unknown_row: dict[str, str] = {
            "id": "unknown",
            "name": "Unknown",
            "language": "Unknown",
            "state": "active",
        }
        body = {"voices": [unknown_row]}
    if scenario == "timed_out":
        failure = httpx2.ReadTimeout("private speech timed out")

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_error: object) -> None:
            return None

        async def get(self, _path: str) -> httpx2.Response:
            if failure is not None:
                raise failure
            return httpx2.Response(
                200,
                json=body,
                request=httpx2.Request("GET", "http://speech.localhost/voices"),
            )

    def transport(**_arguments: object) -> object:
        return object()

    def client(**_arguments: object) -> Client:
        return Client()

    monkeypatch.setattr(speech.httpx2, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(speech.httpx2, "AsyncClient", client)

    async def read() -> None:
        with pytest.raises(VoiceUnavailableError):
            await UdsSpeech(tmp_path / "speech.sock").voices()

    asyncio.run(read())


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
