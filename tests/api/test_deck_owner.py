"""A deck exists only for the account that owns its source."""

from dataclasses import dataclass, replace
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from starlette.websockets import WebSocketDisconnect

from presentator.api.sources import ACCESS, SOURCES
from presentator.contracts.decks import Build, Deck, Source
from presentator.contracts.models import Role
from tests.api.lobby import (
    A_BROWSERS_SOCKET_ADDRESS,
    ADMIN,
    ENGLISH,
    NEIGHBOUR,
    NOW,
    SOURCE_ID,
    GivenDecks,
    Lobby,
    a_configured_source,
    a_lobby,
)
from tests.api.test_sources import confirm_removal
from tests.application.fakes import FakeDeckStore

_TALK: Final = Path(__file__).parent / "built_talk"
_PDF: Final = Path(__file__).parent / "exported_deck.pdf"
_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_B: Final = f"id-of-{NEIGHBOUR}"
_A_SOURCE_URL: Final = "git@heimserver:decks.git"
_B_SOURCE_URL: Final = "https://git.example.invalid/b-talks.git"
_AN_ASSET: Final = "assets/index-mRcVgIkQ.js"
_THE_UNKNOWN: Final = "no-deck"


def _b_source() -> Source:
    return replace(
        a_configured_source(_B_SOURCE_URL),
        id="the-source-of-b",
        name="b-talks",
        owner_id=_B,
    )


def _store_of_built_decks() -> FakeDeckStore:
    store = FakeDeckStore()
    for slug, owner, source_id in (
        ("a-deck", ADMIN, SOURCE_ID),
        ("b-deck", _B, "the-source-of-b"),
    ):
        store.put(
            Deck(
                slug=slug,
                title=f"Title of {slug}",
                changed_at=NOW,
                owner_id=owner,
                source_id=source_id,
                commit=_COMMIT,
                build=None,
                attempt=None,
            ),
        )
        store.put_build(
            slug,
            Build(directory=_TALK, pdf=_PDF, commit=_COMMIT, built_at=NOW),
        )
    return store


@dataclass(frozen=True, slots=True)
class _TwoAdmins:
    lobby: Lobby
    as_a: TestClient
    as_b: TestClient


def _two_admins(*, stuck_mirror: bool = False) -> _TwoAdmins:
    foreign = _b_source()
    lobby = a_lobby(
        given=GivenDecks(
            store=_store_of_built_decks(),
            source=foreign if stuck_mirror else None,
            sources=(a_configured_source(_A_SOURCE_URL), foreign),
            stuck_mirror=stuck_mirror,
        ),
    )
    lobby.sign_in()
    second = lobby.another_browser()
    second.sign_in(username=NEIGHBOUR, role=Role.ADMIN)
    return _TwoAdmins(lobby=lobby, as_a=lobby.client, as_b=second.client)


def _alike(answer: Response, unknown: Response) -> None:
    def comparable(response: Response) -> dict[str, str]:
        return {
            name: value
            for name, value in response.headers.items()
            if name not in {"date", "set-cookie"}
        }

    assert answer.status_code == unknown.status_code
    assert answer.content == unknown.content
    assert comparable(answer) == comparable(unknown)


_NO_HEADERS: Final[dict[str, str]] = {}
_REQUESTS: Final = [
    pytest.param("get", "", _NO_HEADERS, id="the-deck-page"),
    pytest.param("get", "/", _NO_HEADERS, id="the-projector"),
    pytest.param("get", "/presenter/", _NO_HEADERS, id="the-presenter"),
    pytest.param("get", f"/{_AN_ASSET}", _NO_HEADERS, id="an-asset"),
    pytest.param("get", "/no-such-file", _NO_HEADERS, id="the-fallback"),
    pytest.param("head", "/presenter/", _NO_HEADERS, id="head"),
    pytest.param(
        "get",
        f"/{_AN_ASSET}",
        {"range": "bytes=0-9"},
        id="a-range",
    ),
    pytest.param("get", "/pdf", _NO_HEADERS, id="the-pdf"),
]


@pytest.mark.parametrize(("method", "tail", "headers"), _REQUESTS)
def test_a_deck_of_another_account_answers_like_an_unknown_slug(
    method: str,
    tail: str,
    headers: dict[str, str],
) -> None:
    admins = _two_admins()
    send = getattr(admins.as_a, method)

    foreign = send(f"/deck/b-deck{tail}", headers=headers)
    unknown = send(f"/deck/{_THE_UNKNOWN}{tail}", headers=headers)

    _alike(foreign, unknown)
    assert foreign.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("header", ["if-none-match", "if-modified-since"])
def test_a_conditional_request_for_a_foreign_asset_gets_no_not_modified(
    header: str,
) -> None:
    admins = _two_admins()
    own = admins.as_b.get(f"/deck/b-deck/{_AN_ASSET}")
    validators = {
        "if-none-match": own.headers["etag"],
        "if-modified-since": "Fri, 01 Jan 2100 00:00:00 GMT",
    }

    foreign = admins.as_a.get(
        f"/deck/b-deck/{_AN_ASSET}",
        headers={header: validators[header]},
    )
    unknown = admins.as_a.get(
        f"/deck/{_THE_UNKNOWN}/{_AN_ASSET}",
        headers={header: validators[header]},
    )

    _alike(foreign, unknown)
    assert foreign.status_code == HTTPStatus.NOT_FOUND


def test_the_owner_receives_every_shape_of_request_for_the_own_deck() -> None:
    client = _two_admins().as_a
    asset = client.get(f"/deck/a-deck/{_AN_ASSET}")

    assert asset.status_code == HTTPStatus.OK
    assert asset.content == (_TALK / _AN_ASSET).read_bytes()
    assert (
        client.get("/deck/a-deck/no-such-file").content
        == (_TALK / "index.html").read_bytes()
    )
    head = client.head("/deck/a-deck/presenter/")
    assert head.status_code == HTTPStatus.OK
    assert head.content == b""
    part = client.get(f"/deck/a-deck/{_AN_ASSET}", headers={"range": "bytes=0-9"})
    assert part.status_code == HTTPStatus.PARTIAL_CONTENT
    again = client.get(
        f"/deck/a-deck/{_AN_ASSET}",
        headers={"if-none-match": asset.headers["etag"]},
    )
    assert again.status_code == HTTPStatus.NOT_MODIFIED
    assert client.get("/deck/a-deck/pdf").status_code == HTTPStatus.OK


@pytest.mark.parametrize("slug", ["a-deck", "b-deck"])
@pytest.mark.parametrize("signed_in", [True, False])
def test_a_websocket_into_a_talk_is_closed_before_it_is_accepted(
    slug: str,
    *,
    signed_in: bool,
) -> None:
    admins = _two_admins()
    client = admins.as_a if signed_in else admins.lobby.another_browser().client

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"{A_BROWSERS_SOCKET_ADDRESS}/deck/{slug}/"),
    ):
        pass


def test_each_admin_lists_exactly_the_own_deck() -> None:
    admins = _two_admins()

    listed_for_a = admins.as_a.get("/").text
    listed_for_b = admins.as_b.get("/").text

    assert "Title of a-deck" in listed_for_a
    assert "Title of b-deck" not in listed_for_a
    assert "Title of b-deck" in listed_for_b
    assert "Title of a-deck" not in listed_for_b


def test_an_admin_with_no_deck_gets_an_empty_list_that_names_no_address() -> None:
    store = _store_of_built_decks()
    del store.kept["a-deck"]
    lobby = a_lobby(given=GivenDecks(store=store, source=_b_source()))
    lobby.sign_in()

    home = lobby.client.get("/").text

    assert "Title of" not in home
    assert _B_SOURCE_URL not in home


def test_a_member_without_the_admin_role_has_no_deck_of_another_account() -> None:
    lobby = a_lobby(given=GivenDecks(store=_store_of_built_decks()))
    lobby.sign_in(username="walter")

    assert "Title of" not in lobby.client.get("/").text
    for tail in ("", "/presenter/", "/pdf"):
        _alike(
            lobby.client.get(f"/deck/b-deck{tail}"),
            lobby.client.get(f"/deck/{_THE_UNKNOWN}{tail}"),
        )


def test_the_owner_without_the_admin_role_has_the_own_deck_but_no_sources() -> None:
    store = _store_of_built_decks()
    store.put(replace(store.kept["a-deck"], owner_id="id-of-walter"))
    lobby = a_lobby(given=GivenDecks(store=store))
    lobby.sign_in(username="walter")
    client = lobby.client

    assert "Title of a-deck" in client.get("/").text
    for tail in ("", "/presenter/", "/pdf"):
        assert client.get(f"/deck/a-deck{tail}").status_code == HTTPStatus.OK
    assert client.get(SOURCES).status_code == HTTPStatus.FORBIDDEN


def test_a_deck_of_a_removed_source_answers_like_an_unknown_slug_for_its_owner() -> (
    None
):
    admins = _two_admins()
    own_source = a_configured_source(_A_SOURCE_URL)

    removed = confirm_removal(admins.as_a, own_source.name)

    assert removed.status_code == HTTPStatus.SEE_OTHER
    for client in (admins.as_a, admins.as_b):
        _alike(
            client.get("/deck/a-deck/presenter/"),
            client.get(f"/deck/{_THE_UNKNOWN}/presenter/"),
        )
        _alike(client.get("/deck/a-deck"), client.get(f"/deck/{_THE_UNKNOWN}"))


def test_an_admin_opens_the_source_of_another_without_its_deck_chips() -> None:
    admins = _two_admins()

    page = admins.as_a.get(f"{SOURCES}/b-talks")

    assert page.status_code == HTTPStatus.OK
    assert _B_SOURCE_URL in page.text
    assert "b-deck" not in page.text


def test_a_refused_renewal_of_anothers_source_shows_no_deck_chips() -> None:
    admins = _two_admins()

    refused = admins.as_a.post(ACCESS.format(name="b-talks"), data={"secret": "  "})

    assert ENGLISH.source_refused_secret in refused.text
    assert "b-deck" not in refused.text


def test_a_refused_removal_of_anothers_source_shows_no_deck_chips() -> None:
    admins = _two_admins(stuck_mirror=True)

    refused = confirm_removal(admins.as_a, "b-talks")

    assert ENGLISH.source_remove_refused in refused.text
    assert "b-deck" not in refused.text
