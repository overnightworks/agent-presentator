"""The lobby's one body budget: 32 KiB, cut where a body streams in without a length."""

import asyncio
import json
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message
from webauth.cookies import DEFAULT_CSRF_COOKIE_NAME

from presentator.api.auth import BACKCHANNEL_LOGOUT_PATH
from presentator.api.copresenter import MAX_SAID_LENGTH
from presentator.contracts.models import Role
from presentator.contracts.text import DEFAULT_LANGUAGE_TAG
from tests.api.lobby import A_BROWSERS_ADDRESS, a_content_security_policy, a_lobby
from tests.api.test_copresenter import QUESTION, a_copresenter_lobby
from tests.sign_in_support import Scene, a_scene

_FORM = "application/x-www-form-urlencoded"
_JSON = "application/json"
_CHUNK = 4096
_CHUNKS = 16
_CAP = 32 * 1024
_FIRST_CHUNK_PASSING_THE_CAP = _CAP // _CHUNK + 1
_A_NAME = "kleiner-fisch"
_SETTINGS = "/settings"
_ASK = "/copresenter/ask"


@dataclass
class Streamed:
    """What came back from a body streamed in without a `Content-Length`."""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict[str, str])
    chunks_taken: int = 0


def stream_to(app: ASGIApp, path: str, headers: dict[str, str]) -> Streamed:
    """POST sixteen 4 KiB chunks to the app and count the chunks it takes."""
    streamed = Streamed()
    chunks = iter(range(_CHUNKS))

    async def receive() -> Message:
        index = next(chunks, None)
        if index is None:
            return {"type": "http.disconnect"}
        streamed.chunks_taken += 1
        return {
            "type": "http.request",
            "body": b"a" * _CHUNK,
            "more_body": index < _CHUNKS - 1,
        }

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            streamed.status = message["status"]
            streamed.headers = {
                name.decode(): value.decode() for name, value in message["headers"]
            }

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")]
        + [(name.encode(), value.encode()) for name, value in headers.items()],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 50000),
    }

    async def drive() -> None:
        await app(scope, receive, send)

    asyncio.run(drive())
    return streamed


def a_browsers_headers(client: TestClient, content_type: str) -> dict[str, str]:
    cookies = "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar
    )
    return {
        "cookie": cookies,
        "content-type": content_type,
        "origin": A_BROWSERS_ADDRESS,
        "sec-fetch-site": "same-origin",
        "accept": "text/html",
        "x-csrf-token": client.cookies.get(DEFAULT_CSRF_COOKIE_NAME) or "",
    }


def a_settings_form(padding: int) -> str:
    return urlencode(
        {
            "name": _A_NAME + "x" * padding,
            "language": DEFAULT_LANGUAGE_TAG,
            "theme": "dark",
        }
    )


def test_a_streamed_body_over_the_cap_on_the_back_channel_gets_413(
    tmp_path: Path,
) -> None:
    scene: Scene = a_scene(tmp_path)
    scene.sign_in()

    streamed = stream_to(
        scene.lobby.app, BACKCHANNEL_LOGOUT_PATH, {"content-type": _FORM}
    )

    assert streamed.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert streamed.headers["cache-control"] == "no-store"
    assert streamed.chunks_taken <= _FIRST_CHUNK_PASSING_THE_CAP
    assert scene.count("sessions") == 1


def test_a_streamed_settings_form_over_the_cap_gets_400_and_saves_nothing() -> None:
    admin = a_lobby()
    admin.sign_in()

    streamed = stream_to(admin.app, _SETTINGS, a_browsers_headers(admin.client, _FORM))

    assert streamed.status == HTTPStatus.BAD_REQUEST
    assert streamed.chunks_taken <= _FIRST_CHUNK_PASSING_THE_CAP
    assert _A_NAME not in admin.client.get(_SETTINGS).text


def test_a_streamed_ask_over_the_cap_gets_400_and_reaches_no_private_service() -> None:
    lobby, private, _clock = a_copresenter_lobby()
    lobby.sign_in()

    streamed = stream_to(lobby.app, _ASK, a_browsers_headers(lobby.client, _JSON))

    assert streamed.status == HTTPStatus.BAD_REQUEST
    assert streamed.chunks_taken <= _FIRST_CHUNK_PASSING_THE_CAP
    assert private.questions == []


def test_the_longest_ask_in_the_widest_json_spelling_still_fits_the_cap() -> None:
    lobby, private, _clock = a_copresenter_lobby()
    lobby.sign_in()
    escaped_surrogate_pair = "\\ud83d\\ude00"
    body = json.dumps({**QUESTION, "said": "SAID"}).replace(
        "SAID", escaped_surrogate_pair * MAX_SAID_LENGTH
    )
    headers = a_browsers_headers(lobby.client, _JSON)

    answer = lobby.client.post(_ASK, content=body, headers=headers)

    assert len(body) > _CAP // 2
    assert answer.status_code == HTTPStatus.OK
    (asked,) = private.questions
    assert asked.said == "\N{GRINNING FACE}" * MAX_SAID_LENGTH


def test_a_33_kib_settings_form_gets_413_with_the_security_headers() -> None:
    admin = a_lobby()
    admin.sign_in(role=Role.ADMIN)

    answer = admin.client.post(
        _SETTINGS,
        content=a_settings_form(33 * 1024),
        headers={"content-type": _FORM},
    )

    assert answer.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert answer.headers["content-security-policy"] == a_content_security_policy()
    assert answer.headers["cache-control"] == "no-store"
    assert _A_NAME not in admin.client.get(_SETTINGS).text
