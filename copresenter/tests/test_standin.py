"""The speech stand-in answers the three endpoints of the issue 74 contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from copresenter.standin import create_standin
from copresenter.wav import SAMPLE_RATE


def test_health_names_the_stand_in_and_says_ready() -> None:
    body = TestClient(create_standin()).get("/health").json()

    assert body["speaking"]["ready"] is True
    assert body["hearing"]["ready"] is True
    assert body["speaking"]["model"] == "stand-in"
    assert body["sample_rate"] == SAMPLE_RATE


def test_speak_returns_chunked_wav() -> None:
    client = TestClient(create_standin())
    with client.stream("POST", "/speak", json={"text": "Hallo.", "language": "de"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        body = b"".join(response.iter_bytes())

    assert body[:4] == b"RIFF"
    assert body[8:12] == b"WAVE"
    assert len(body) > 44
