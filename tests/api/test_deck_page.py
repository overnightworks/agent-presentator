"""One deck's page, the talk it leads into, and its PDF, as a browser gets them."""

from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from presentator.contracts.decks import Build, Deck
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
_EXPORTED_PDF: Final = Path(__file__).parent / "exported_deck.pdf"
_PAGE: Final = f"/deck/{_SLUG}"
_PROJECTOR: Final = f"/deck/{_SLUG}/"
_PRESENTER: Final = f"/deck/{_SLUG}/presenter/"
_PDF: Final = f"/deck/{_SLUG}/pdf"
_LOGIN: Final = "/login"
_PDF_TYPE: Final = "application/pdf"
_SAVED_AS: Final = f'attachment; filename="{_SLUG}.pdf"'
_ONLY_THIS_TEST_WROTE_IT: Final = "the secret beside the build directory"
_BUILT_AGO: Final = timedelta(minutes=12)
_HOW_LONG_AGO: Final = "12 minutes ago"


def a_build(*, talk: Path = _BUILT_TALK, pdf: Path = _EXPORTED_PDF) -> Build:
    """A build that switched over a while ago, as the picture's board shows it."""
    return Build(directory=talk, pdf=pdf, commit=_COMMIT, built_at=NOW - _BUILT_AGO)


def a_deck_store(
    *,
    slug: str = _SLUG,
    built: Build | None = None,
    owner: str = ADMIN,
) -> FakeDeckStore:
    store = FakeDeckStore()
    store.put(
        Deck(
            slug=slug,
            title=_TITLE,
            changed_at=NOW,
            owner_id=owner,
            commit=_COMMIT,
            build=None,
        ),
    )
    if built is not None:
        store.put_build(slug, built)
    return store


@pytest.fixture
def lobby() -> TestClient:
    return a_signed_in_lobby(
        store=a_deck_store(built=a_build()),
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


def test_a_deck_with_an_exported_pdf_offers_it_for_download_on_its_page(
    lobby: TestClient,
) -> None:
    page = lobby.get(_PAGE).text

    assert f'href="{_PDF}"' in page
    assert TEXT.deck_pdf in page


def test_the_download_hands_the_file_the_deck_names_over_to_be_saved(
    lobby: TestClient,
) -> None:
    download = lobby.get(_PDF)

    assert download.status_code == HTTPStatus.OK
    assert download.headers["content-type"] == _PDF_TYPE
    assert download.headers["content-disposition"] == _SAVED_AS
    assert download.content == _EXPORTED_PDF.read_bytes()


def test_a_built_decks_page_says_how_long_ago_its_talk_was_built() -> None:
    signed_in = a_signed_in_lobby(
        store=a_deck_store(built=a_build()),
        source=a_configured_source(_ADDRESS),
    )

    page = signed_in.get(_PAGE).text

    assert TEXT.deck_built.format(age=_HOW_LONG_AGO) in page
    assert _SHORT_COMMIT in page


@pytest.mark.parametrize(
    "built",
    [None, a_build(pdf=Path("/var/lib/presentator/builds/nothing-was-written.pdf"))],
    ids=["nothing built", "a file that is gone"],
)
def test_a_download_with_nothing_behind_it_answers_the_lobbys_own_page(
    built: Build | None,
) -> None:
    signed_in = a_signed_in_lobby(
        store=a_deck_store(built=built),
        source=a_configured_source(_ADDRESS),
    )

    missing = signed_in.get(_PDF)

    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert TEXT.deck_unknown_title in missing.text
    assert "detail" not in missing.text


@pytest.mark.parametrize(
    "slug",
    ["hello\\deck", 'hello"deck', "hello\ndeck"],
    ids=["a separator", "a quote", "a line break"],
)
def test_a_slug_that_could_reach_a_header_is_refused_before_one_is_built(
    slug: str,
) -> None:
    signed_in = a_signed_in_lobby(
        store=a_deck_store(slug=slug, built=a_build()),
        source=a_configured_source(_ADDRESS),
    )

    refused = signed_in.get(f"/deck/{quote(slug, safe='')}/pdf")

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert "content-disposition" not in refused.headers
    assert TEXT.deck_unknown_title in refused.text


def test_a_deck_nothing_has_been_built_from_says_so_and_offers_no_view(
    unbuilt_lobby: TestClient,
) -> None:
    page = unbuilt_lobby.get(_PAGE)

    assert page.status_code == HTTPStatus.OK
    assert TEXT.deck_not_built_explanation in page.text
    assert TEXT.deck_state_not_built in page.text
    assert _PRESENTER not in page.text
    assert _PROJECTOR not in page.text
    assert TEXT.deck_pdf not in page.text


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
    [_PAGE, _PROJECTOR, _PRESENTER, f"{_PROJECTOR}index.html", _PDF],
    ids=["page", "projector", "presenter", "an asset", "the pdf"],
)
def test_a_talk_answers_the_login_to_anyone_who_is_not_signed_in(address: str) -> None:
    signed_out = a_lobby(
        store=a_deck_store(built=a_build()),
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
        store=a_deck_store(built=a_build(talk=a_build_next_to_a_secret(tmp_path))),
        source=a_configured_source(_ADDRESS),
    )

    refused = signed_in.get(crafted)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert _ONLY_THIS_TEST_WROTE_IT not in refused.text


def test_the_talk_itself_is_still_served_from_inside_that_directory(
    tmp_path: Path,
) -> None:
    signed_in = a_signed_in_lobby(
        store=a_deck_store(built=a_build(talk=a_build_next_to_a_secret(tmp_path))),
        source=a_configured_source(_ADDRESS),
    )

    assert "a talk" in signed_in.get(_PROJECTOR).text


def test_a_deck_another_person_owns_is_held_by_anyone_signed_in() -> None:
    lobby = a_signed_in_lobby(
        store=a_deck_store(built=a_build(), owner="someone-else"),
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


def test_a_deck_removed_by_reconciliation_answers_the_lobbys_not_found() -> None:
    store = a_deck_store(built=a_build())
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
