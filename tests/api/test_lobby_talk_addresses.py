"""The lobby serves no talk: every old talk address answers as if none stood there."""

from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from presentator.api.talk import talk_content_security_policy
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_SOCKET_ADDRESS,
    a_content_security_policy,
)
from tests.api.talk_support import A_DECK, B_DECK, NO_DECK, two_hosts

_BUILT_TALK: Final = Path(__file__).parent / "built_talk"
_AN_ASSET: Final = "assets/index-mRcVgIkQ.js"
_THE_THREE: Final = [A_DECK, B_DECK, NO_DECK]
_SHAPES_OF_A_TALK: Final = [
    "presenter/",
    "presenter/2",
    "index.html",
    _AN_ASSET,
    "no-such-file",
]
_SHAPES_AROUND_A_TALK: Final = [
    "",
    "presenter/",
    "presenter/2",
    "index.html",
    _AN_ASSET,
    "no-such-file",
    "pdf/",
]


@pytest.fixture
def lobby(tmp_path: Path) -> TestClient:
    return two_hosts(tmp_path).signed_in_at(A_BROWSERS_ADDRESS)


def _unvarying(response: Response, *, besides: frozenset[str] = frozenset()) -> str:
    """What an answer says apart from its date, its cookie values and `besides`."""
    withheld = {"date", "set-cookie", *besides}
    headers = {
        name: value
        for name, value in sorted(response.headers.items())
        if name not in withheld
    }
    return repr((response.status_code, response.content, headers))


# C10.
@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("shape", _SHAPES_OF_A_TALK)
def test_a_talk_address_on_the_lobby_answers_not_found_alike_for_every_slug(
    lobby: TestClient, method: str, shape: str
) -> None:
    answers = [getattr(lobby, method)(f"/deck/{slug}/{shape}") for slug in _THE_THREE]

    assert {answer.status_code for answer in answers} == {HTTPStatus.NOT_FOUND}
    assert len({_unvarying(answer) for answer in answers}) == 1


# C11.
@pytest.mark.parametrize("method", ["get", "head"])
def test_the_slash_address_of_a_deck_on_the_lobby_redirects_alike_for_every_slug(
    lobby: TestClient, method: str
) -> None:
    answers = {slug: getattr(lobby, method)(f"/deck/{slug}/") for slug in _THE_THREE}

    assert {answer.status_code for answer in answers.values()} == {
        HTTPStatus.TEMPORARY_REDIRECT
    }
    for slug, answer in answers.items():
        assert answer.headers["location"] == f"{A_BROWSERS_ADDRESS}/deck/{slug}"
    assert (
        len({_unvarying(a, besides=frozenset({"location"})) for a in answers.values()})
        == 1
    )


# C12.
@pytest.mark.parametrize("slug", [A_DECK, B_DECK])
@pytest.mark.parametrize("address", ["/", "/presenter/"])
@pytest.mark.parametrize("signed_in", [True, False])
def test_a_websocket_into_a_deck_address_on_the_lobby_is_never_accepted(
    tmp_path: Path, slug: str, address: str, *, signed_in: bool
) -> None:
    hosts = two_hosts(tmp_path)
    browser = (
        hosts.signed_in_at(A_BROWSERS_ADDRESS)
        if signed_in
        else hosts.browser(A_BROWSERS_ADDRESS)
    )

    with (
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
        browser.websocket_connect(f"{A_BROWSERS_SOCKET_ADDRESS}/deck/{slug}{address}"),
    ):
        pytest.fail("a socket was accepted")


# C15.
@pytest.mark.parametrize(
    "address",
    [
        *(f"/deck/{A_DECK}/{shape}" for shape in _SHAPES_AROUND_A_TALK),
        "/static/%2e%2e/deck/a-deck/",
        f"/deck/{A_DECK}/pdf/",
        f"/deck/{A_DECK}",
    ],
)
def test_no_lobby_address_of_a_deck_returns_a_byte_of_its_built_talk(
    lobby: TestClient, address: str
) -> None:
    answer = lobby.get(address)

    built = [
        (_BUILT_TALK / "index.html").read_bytes(),
        (_BUILT_TALK / _AN_ASSET).read_bytes(),
    ]
    assert all(part not in answer.content for part in built)


# C16.
def test_the_lobby_policy_admits_no_inline_style_and_the_talk_host_still_does(
    lobby: TestClient,
) -> None:
    on_the_lobby = lobby.get("/").headers["content-security-policy"]

    assert on_the_lobby == a_content_security_policy()
    style_source = next(
        directive for directive in on_the_lobby.split(";") if "style-src" in directive
    )
    assert "'unsafe-inline'" not in style_source
    assert "'unsafe-inline'" in talk_content_security_policy()
