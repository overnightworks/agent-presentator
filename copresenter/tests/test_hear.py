"""Hearing proxy: errors are named, and either direction ending stops both."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copresenter.answer import CannedAnswerer
from copresenter.app import create_app
from copresenter.config import Settings

from .conftest import EXAMPLE_DECK, FakeSpeech


class EndingUpstream:
    """A speech socket that finishes as soon as it is opened."""

    def __init__(self) -> None:
        self.closed = 0
        self.sent: list[bytes] = []

    def __aiter__(self) -> EndingUpstream:
        """Yield this connection as an async iterator."""
        return self

    async def __anext__(self) -> str:
        """End immediately so the downward proxy completes."""
        raise StopAsyncIteration

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed += 1


class EndingSpeech(FakeSpeech):
    """A speech port whose hearing socket is already finished."""

    def __init__(self) -> None:
        super().__init__()
        self.upstream = EndingUpstream()

    async def open_hear(self, language: str) -> EndingUpstream:
        del language
        return self.upstream


def test_hear_reports_unavailable_when_speech_cannot_open(app) -> None:
    client = TestClient(app)
    with client.websocket_connect("/hear?language=de") as socket:
        payload = socket.receive_json()

    assert payload["error"] == "hearing unavailable"
    assert payload["final"] is True


def test_hear_proxy_closes_when_the_speech_socket_ends(example_deck) -> None:
    speech = EndingSpeech()
    app = create_app(
        Settings(deck=EXAMPLE_DECK, speech_url="http://speech.test"),
        deck=example_deck,
        speech=speech,
        answerer=CannedAnswerer(),
    )
    client = TestClient(app)
    with (
        client.websocket_connect("/hear?language=de") as socket,
        pytest.raises(WebSocketDisconnect),
    ):
        socket.receive_text()

    assert speech.upstream.closed >= 1
