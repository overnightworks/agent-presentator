"""One deck's page, and the built talk it leads into, as a browser receives them."""

from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

from presentator.contracts.decks import Deck
from tests.api.lobby import (
    ADMIN,
    NOW,
    TEXT,
    a_configured_source,
    a_lobby,
    a_signed_in_lobby,
)
from tests.application.fakes import FakeDeckStore

_SLUG: Final = "hello-deck"
_TITLE: Final = "Hello Co-Presenter"
_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_SHORT_COMMIT: Final = "a3f19c2"
_ADDRESS: Final = "git@heimserver:decks.git"
_BUILT_TALK: Final = Path(__file__).parent / "built_talk"
_PAGE: Final = f"/deck/{_SLUG}"
_PROJECTOR: Final = f"/deck/{_SLUG}/"
_PRESENTER: Final = f"/deck/{_SLUG}/presenter/"
_LOGIN: Final = "/login"
_ONLY_THIS_TEST_WROTE_IT: Final = "the secret beside the build directory"


def a_deck_store(
    *,
    built_talk: Path | None = None,
    owner: str = ADMIN,
) -> FakeDeckStore:
    store = FakeDeckStore()
    store.put(
        Deck(
            slug=_SLUG,
            title=_TITLE,
            changed_at=NOW,
            owner_id=owner,
            commit=_COMMIT,
            active_build=None,
        ),
    )
    if built_talk is not None:
        store.put_active_build(_SLUG, directory=built_talk)
    return store


@pytest.fixture
def lobby() -> TestClient:
    return a_signed_in_lobby(
        store=a_deck_store(built_talk=_BUILT_TALK),
        source=a_configured_source(_ADDRESS),
    )


@pytest.fixture
def unbuilt_lobby() -> TestClient:
    return a_signed_in_lobby(
        store=a_deck_store(),
        source=a_configured_source(_ADDRESS),
    )


def a_build_next_to_a_secret(tmp_path: Path) -> Path:
    """A build directory with something worth stealing outside and beside it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(_ONLY_THIS_TEST_WROTE_IT, encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<h1>a talk</h1>", encoding="utf-8")
    (build / "out").symlink_to(outside)
    return build


def test_the_deck_page_names_the_title_the_source_and_the_commit_it_was_taken_from(
    lobby: TestClient,
) -> None:
    page = lobby.get(_PAGE)

    assert page.status_code == HTTPStatus.OK
    assert _TITLE in page.text
    assert _SLUG in page.text
    assert _ADDRESS in page.text
    assert _SHORT_COMMIT in page.text
    assert _COMMIT not in page.text


def test_a_deck_with_a_built_talk_opens_its_presenter_and_its_projector_view(
    lobby: TestClient,
) -> None:
    page = lobby.get(_PAGE).text

    assert f'href="{_PRESENTER}"' in page
    assert f'href="{_PROJECTOR}"' in page
    assert TEXT.deck_state_ready in page
    assert lobby.get(_PRESENTER).status_code == HTTPStatus.OK
    assert lobby.get(_PROJECTOR).status_code == HTTPStatus.OK


def test_a_talk_is_served_with_the_slide_in_the_address_it_was_left_at(
    lobby: TestClient,
) -> None:
    projector = lobby.get(_PROJECTOR)

    assert "location.hash" in projector.text
    assert projector.headers["content-type"].startswith("text/html")


def test_a_deck_nothing_has_been_built_from_says_so_and_offers_no_view(
    unbuilt_lobby: TestClient,
) -> None:
    page = unbuilt_lobby.get(_PAGE)

    assert page.status_code == HTTPStatus.OK
    assert TEXT.deck_not_built_explanation in page.text
    assert TEXT.deck_state_not_built in page.text
    assert _PRESENTER not in page.text
    assert _PROJECTOR not in page.text


@pytest.mark.parametrize(
    "address", [_PROJECTOR, _PRESENTER], ids=["projector", "presenter"]
)
def test_a_view_of_a_deck_nothing_has_been_built_from_answers_nothing(
    unbuilt_lobby: TestClient,
    address: str,
) -> None:
    assert unbuilt_lobby.get(address).status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "address",
    [_PAGE, _PROJECTOR, _PRESENTER, f"{_PROJECTOR}index.html"],
    ids=["page", "projector", "presenter", "an asset"],
)
def test_a_talk_answers_the_login_to_anyone_who_is_not_signed_in(address: str) -> None:
    signed_out = a_lobby(
        store=a_deck_store(built_talk=_BUILT_TALK),
        source=a_configured_source(_ADDRESS),
    )

    refused = signed_out.get(address)

    assert refused.status_code == HTTPStatus.FOUND
    assert refused.headers["location"] == _LOGIN
    assert _TITLE not in refused.text


@pytest.mark.parametrize(
    "crafted",
    [
        "/deck/hello-deck/%2e%2e/outside/secret.txt",
        "/deck/hello-deck/..%2foutside%2fsecret.txt",
        "/deck/hello-deck/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/deck/hello-deck/out/secret.txt",
    ],
    ids=["encoded dots", "encoded separator", "out of the tree", "symlink"],
)
def test_a_path_that_would_leave_the_build_directory_is_refused(
    tmp_path: Path,
    crafted: str,
) -> None:
    signed_in = a_signed_in_lobby(
        store=a_deck_store(built_talk=a_build_next_to_a_secret(tmp_path)),
        source=a_configured_source(_ADDRESS),
    )

    refused = signed_in.get(crafted)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert _ONLY_THIS_TEST_WROTE_IT not in refused.text


def test_the_talk_itself_is_still_served_from_inside_that_directory(
    tmp_path: Path,
) -> None:
    signed_in = a_signed_in_lobby(
        store=a_deck_store(built_talk=a_build_next_to_a_secret(tmp_path)),
        source=a_configured_source(_ADDRESS),
    )

    assert "a talk" in signed_in.get(_PROJECTOR).text


def test_a_deck_another_person_owns_is_held_by_anyone_signed_in() -> None:
    lobby = a_signed_in_lobby(
        store=a_deck_store(built_talk=_BUILT_TALK, owner="someone-else"),
        source=a_configured_source(_ADDRESS),
    )

    assert lobby.get(_PAGE).status_code == HTTPStatus.OK
    assert lobby.get(_PRESENTER).status_code == HTTPStatus.OK


def test_an_address_no_deck_carries_answers_the_lobbys_own_page(
    lobby: TestClient,
) -> None:
    unknown = lobby.get("/deck/nothing-was-pushed-here")

    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert TEXT.deck_unknown_title in unknown.text
    assert "detail" not in unknown.text


def test_a_deck_page_stands_behind_the_lobbys_header(lobby: TestClient) -> None:
    page = lobby.get(_PAGE).text

    assert f'aria-current="page">{TEXT.section_decks}<' in page
    assert TEXT.log_out in page


def test_a_deck_removed_by_reconciliation_answers_the_lobbys_not_found(
    lobby: TestClient,
) -> None:
    store = a_deck_store(built_talk=_BUILT_TALK)
    signed_in = a_signed_in_lobby(store=store, source=a_configured_source(_ADDRESS))

    store.mark_removed_except(present=frozenset(), at=NOW)
    removed = signed_in.get(_PAGE)

    store.mark_removed_except(present=frozenset({_SLUG}), at=NOW)
    returned = signed_in.get(_PAGE)

    assert removed.status_code == HTTPStatus.NOT_FOUND
    assert TEXT.deck_unknown_title in removed.text
    assert returned.status_code == HTTPStatus.OK
    assert TEXT.deck_state_ready in returned.text
    assert signed_in.get(_PRESENTER).status_code == HTTPStatus.OK
