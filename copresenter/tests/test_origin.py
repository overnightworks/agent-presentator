"""The service answers one named origin and no other, and needs to be told which."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from copresenter.config import Settings

from .conftest import ALLOWED_ORIGIN

OTHER_ORIGIN = "https://elsewhere.test"


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


def test_another_origin_is_refused(app) -> None:
    response = TestClient(app).options(
        "/ask",
        headers={
            "Origin": OTHER_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_a_reply_to_another_origin_carries_no_permission(app) -> None:
    response = TestClient(app).get("/who", headers={"Origin": OTHER_ORIGIN})

    assert "access-control-allow-origin" not in response.headers


def test_a_page_on_another_origin_cannot_open_the_hearing_socket(app) -> None:
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as refusal,
        client.websocket_connect("/hear?language=de", headers={"Origin": OTHER_ORIGIN}),
    ):
        pass

    assert refusal.value.code == 1008


def test_without_the_setting_the_service_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COPRESENTER_ALLOWED_ORIGIN", raising=False)

    with pytest.raises(ValidationError) as refusal:
        Settings()

    assert "COPRESENTER_ALLOWED_ORIGIN" in str(refusal.value)
