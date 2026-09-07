"""WS /hear sends partials, then a final, and a second utterance without reload."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from speech.config import HEAR_SAMPLE_RATE
from speech.pcm import BYTES_PER_SAMPLE
from tests.conftest import FakeHearing, an_app, sine_pcm


def _silence(seconds: float) -> bytes:
    return bytes(int(seconds * HEAR_SAMPLE_RATE * BYTES_PER_SAMPLE))


def test_hear_sends_partials_then_a_final_and_a_second_utterance() -> None:
    hearing = FakeHearing()
    client = TestClient(an_app(hearing=hearing))
    speech = sine_pcm(0.5)
    pause = _silence(0.8)

    with client.websocket_connect("/hear?language=de") as socket:
        socket.send_bytes(speech)
        first = socket.receive_json()
        assert first["final"] is False
        assert "eins" in first["text"]
        socket.send_bytes(pause)
        final = socket.receive_json()
        assert final["final"] is True
        socket.send_bytes(speech)
        second = socket.receive_json()
        assert second["final"] is False
        assert "eins" in second["text"]

    assert hearing.sessions == 1


def test_hear_closes_while_the_model_is_not_ready() -> None:
    client = TestClient(an_app(hearing=FakeHearing(ready=False)))

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/hear?language=de",
        ) as socket,
    ):
        socket.receive_json()
