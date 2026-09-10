"""The service works for one named origin, refuses every other caller, and needs it named."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from copresenter.app import create_app
from copresenter.config import Settings, Transport

from .conftest import ALLOWED_ORIGIN

OTHER_ORIGIN = "https://elsewhere.test"
FORBIDDEN = 403
POLICY_VIOLATION = 1008
QUESTION = {"said": "Worum geht es hier?", "slide": 1}


def test_the_named_origin_may_ask(app) -> None:
    response = TestClient(app).options(
        "/ask",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "access-control-allow-credentials" not in response.headers


def test_the_named_origin_is_answered_and_spoken(client, fake_speech, answerer) -> None:
    response = client.post("/ask", json=QUESTION)

    assert response.status_code == 200
    assert answerer.turns == 1
    assert fake_speech.spoken != []


def test_the_named_origin_is_told_who_answers(client, fake_speech) -> None:
    response = client.get("/who")

    assert response.status_code == 200
    assert fake_speech.health_asks == 1


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": OTHER_ORIGIN}, id="another origin"),
        pytest.param({}, id="no origin at all"),
    ],
)
def test_only_that_origin_reaches_the_answerer(app, fake_speech, answerer, headers) -> None:
    client = TestClient(app)

    asked = client.post("/ask", json=QUESTION, headers=headers)
    asked_who = client.get("/who", headers=headers)

    assert asked.status_code == FORBIDDEN
    assert asked_who.status_code == FORBIDDEN
    assert answerer.turns == 0
    assert fake_speech.spoken == []
    assert fake_speech.health_asks == 0
    assert fake_speech.hear_opens == 0


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": OTHER_ORIGIN}, id="another origin"),
        pytest.param({}, id="no origin at all"),
    ],
)
def test_only_that_origin_opens_the_hearing_socket(app, fake_speech, headers) -> None:
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as refusal,
        client.websocket_connect("/hear?language=de", headers=headers),
    ):
        pass

    assert refusal.value.code == POLICY_VIOLATION
    assert fake_speech.hear_opens == 0


def test_the_named_origin_opens_the_hearing_socket(client, fake_speech) -> None:
    with client.websocket_connect("/hear?language=de") as socket:
        payload = socket.receive_json()

    assert fake_speech.hear_opens == 1
    assert payload["error"] == "hearing unavailable"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("*", id="wildcard"),
        pytest.param("", id="blank"),
        pytest.param("presentator.test", id="no scheme"),
        pytest.param("https://presentator.test/deck", id="a path"),
        pytest.param("https://user:pass@presentator.test", id="credentials"),
        pytest.param("https://presentator.test?a=1", id="a query"),
        pytest.param("ws://presentator.test", id="another scheme"),
    ],
)
def test_a_value_that_is_not_one_origin_refuses_to_start(value) -> None:
    with pytest.raises(ValidationError) as refusal:
        Settings(allowed_origin=value)

    assert "COPRESENTER_ALLOWED_ORIGIN" in str(refusal.value)


def test_an_origin_with_a_port_is_one_origin() -> None:
    assert (
        Settings(allowed_origin="http://127.0.0.1:3030").allowed_origin == "http://127.0.0.1:3030"
    )


def test_without_the_setting_the_service_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COPRESENTER_ALLOWED_ORIGIN", raising=False)

    with pytest.raises(ValidationError) as refusal:
        Settings()

    assert "COPRESENTER_ALLOWED_ORIGIN" in str(refusal.value)


def test_unix_composition_needs_no_browser_origin(
    tmp_path,
    example_deck,
    fake_speech,
    answerer,
) -> None:
    settings = Settings(
        transport=Transport.UNIX,
        socket_directory=tmp_path,
        runtime_uid=os.geteuid(),
        deck=example_deck.source.parent,
        speech_url="http://speech.test",
    )
    app = create_app(
        settings,
        deck=example_deck,
        speech=fake_speech,
        answerer=answerer,
    )
    client = TestClient(app)

    assert client.get("/who").status_code == 200
    with client.websocket_connect("/hear?language=de") as hearing:
        assert hearing.receive_json()["error"] == "hearing unavailable"
