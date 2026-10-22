"""One deck's page, the talk it leads into, and its PDF, as a browser gets them."""

import shutil
from dataclasses import replace
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from presentator.contracts.decks import Build, BuildAttempt, BuildOutcome, Deck
from presentator.contracts.text import Catalogs
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_TALK_ADDRESS,
    ADMIN,
    BUILT_TALK,
    ENGLISH,
    NOW,
    SOURCE_ID,
    GivenDecks,
    a_configured_source,
    a_lobby,
    a_signed_in_lobby,
    a_swapped_build,
)
from tests.api.talk_support import ONLY_THIS_TEST_WROTE_IT, two_hosts
from tests.application.fakes import FakeDeckStore

_SLUG: Final = "hello-deck"
_TITLE: Final = "Hello Co-Presenter"
_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_SHORT_COMMIT: Final = "a3f19c2"
_ADDRESS: Final = "git@heimserver:decks.git"
# What `slidev build` of `examples/hello-deck` writes, `--base /deck/hello-deck/`.
_EXPORTED_PDF: Final = Path(__file__).parent / "exported_deck.pdf"
_PAGE: Final = f"/deck/{_SLUG}"
_PROJECTOR: Final = f"/deck/{_SLUG}/"
_PRESENTER: Final = f"/deck/{_SLUG}/presenter/"
_PDF: Final = f"/deck/{_SLUG}/pdf"
_PDF_TYPE: Final = "application/pdf"
_SAVED_AS: Final = f'attachment; filename="{_SLUG}.pdf"'
_BUILT_AGO: Final = timedelta(minutes=12)
_HOW_LONG_AGO: Final = "12 minutes ago"
_ATTEMPT_COMMIT: Final = "e91c5ad4f3b2a1908877665544332211aabbccdd"
_SHORT_ATTEMPT_COMMIT: Final = "e91c5ad"
_TRIED_AGO: Final = timedelta(minutes=26)
_HOW_LONG_SINCE_THE_ATTEMPT: Final = "26 minutes ago"
_RUNNING_FOR: Final = timedelta(seconds=40)
_HOW_LONG_IT_HAS_RUN: Final = "40 seconds"
_WHAT_THE_TOOLCHAIN_SAID: Final = "slides.md:41:3 Unexpected token in frontmatter"
_A_DECK_THAT_WRITES_MARKUP: Final = "<script>alert('slides')</script>"
_THEMES: Final = ("apple-basic", "bricks", "default", "seriph", "shibainu")


def a_build(*, talk: Path = BUILT_TALK, pdf: Path = _EXPORTED_PDF) -> Build:
    """A build that switched over a while ago, as the picture's board shows it."""
    return Build(directory=talk, pdf=pdf, commit=_COMMIT, built_at=NOW - _BUILT_AGO)


def a_running_attempt() -> BuildAttempt:
    """A build that said it began and has not reported back, as the picture shows."""
    return BuildAttempt(
        commit=_ATTEMPT_COMMIT,
        started_at=NOW - _RUNNING_FOR,
        outcome=BuildOutcome.RUNNING,
        failure=None,
    )


def a_failed_attempt(*, said: str | None = _WHAT_THE_TOOLCHAIN_SAID) -> BuildAttempt:
    """A build that broke a while ago, with or without words of its own."""
    return BuildAttempt(
        commit=_ATTEMPT_COMMIT,
        started_at=NOW - _TRIED_AGO,
        outcome=BuildOutcome.FAILED,
        failure=said,
    )


def a_deck_store(
    *,
    slug: str = _SLUG,
    built: Build | None = None,
    attempt: BuildAttempt | None = None,
    owner: str = ADMIN,
) -> FakeDeckStore:
    store = FakeDeckStore()
    store.put(
        Deck(
            slug=slug,
            title=_TITLE,
            changed_at=NOW,
            owner_id=owner,
            source_id=SOURCE_ID,
            commit=_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    if built is not None:
        store.put_build(slug, built)
    if attempt is not None:
        store.put_attempt(slug, attempt)
    return store


def a_page_of_a_deck(
    *,
    built: Build | None = None,
    attempt: BuildAttempt | None = None,
) -> str:
    """The deck page as a signed-in person receives it in that state."""
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=built, attempt=attempt),
            source=a_configured_source(_ADDRESS),
        ),
    )
    return signed_in.get(_PAGE).text


def a_talk_host(tmp_path: Path, given: GivenDecks) -> TestClient:
    """A browser signed in at the talk host of an instance with these decks."""
    return two_hosts(tmp_path, given=given).signed_in_at(A_TALK_ADDRESS)


@pytest.fixture
def lobby() -> TestClient:
    return a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
        ),
    )


@pytest.fixture
def unbuilt_lobby() -> TestClient:
    return a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(),
            source=a_configured_source(_ADDRESS),
        ),
    )


@pytest.fixture
def unbuilt_talk_host(tmp_path: Path) -> TestClient:
    return a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(),
            source=a_configured_source(_ADDRESS),
        ),
    )


@pytest.fixture
def talk_host(tmp_path: Path) -> TestClient:
    return a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
        ),
    )


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


# C13.
@pytest.mark.parametrize(
    "view", [_PRESENTER, _PROJECTOR], ids=["presenter", "projector"]
)
def test_a_deck_with_a_built_talk_links_its_views_at_the_talk_host(
    lobby: TestClient, view: str
) -> None:
    page = lobby.get(_PAGE).text

    assert f'href="{A_TALK_ADDRESS}{view}"' in page
    assert ENGLISH.deck_state_ready in page


# D4.
def test_a_built_talk_opens_its_presenter_and_its_projector_view_at_the_talk_host(
    talk_host: TestClient,
) -> None:
    application = (BUILT_TALK / "index.html").read_bytes()

    projector = talk_host.get(_PROJECTOR)
    presenter = talk_host.get(_PRESENTER)

    assert projector.status_code == HTTPStatus.OK
    assert presenter.status_code == HTTPStatus.OK
    assert projector.content == application
    assert presenter.content == application


def test_a_talk_is_served_with_the_slide_in_the_address_it_was_left_at(
    talk_host: TestClient,
) -> None:
    application = (BUILT_TALK / "index.html").read_bytes()
    slide = talk_host.get(f"{_PROJECTOR}2")
    presenter_slide = talk_host.get(f"{_PRESENTER}2")

    assert slide.status_code == HTTPStatus.OK
    assert presenter_slide.status_code == HTTPStatus.OK
    assert slide.content == application
    assert presenter_slide.content == application
    assert slide.headers["content-type"].startswith("text/html")


def test_a_deck_with_an_exported_pdf_offers_it_for_download_on_its_page(
    lobby: TestClient,
) -> None:
    page = lobby.get(_PAGE).text

    assert f'href="{_PDF}"' in page
    assert ENGLISH.deck_pdf in page


def test_the_download_hands_the_file_the_deck_names_over_to_be_saved(
    lobby: TestClient,
) -> None:
    download = lobby.get(_PDF)

    assert download.status_code == HTTPStatus.OK
    assert download.headers["content-type"] == _PDF_TYPE
    assert download.headers["content-disposition"] == _SAVED_AS
    assert download.content == _EXPORTED_PDF.read_bytes()


def test_the_deck_page_names_the_themes_this_instance_builds_with() -> None:
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
            themes=_THEMES,
        ),
    )

    page = signed_in.get(_PAGE).text

    assert ENGLISH.deck_builds_with.format(themes=", ".join(_THEMES)) in page
    start = page.index("data-builds-with")
    row = page[start : page.index("</p>", start)]
    assert all(theme in row for theme in _THEMES)
    # Names only (R4): the row that carries them has no path and no version.
    assert "/" not in row
    assert not any(character.isdigit() for character in row)


def test_the_deck_page_omits_the_row_while_the_toolchain_cannot_be_read() -> None:
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
            themes=None,
        ),
    )

    page = signed_in.get(_PAGE).text

    assert "data-builds-with" not in page


def test_the_builds_with_row_stays_one_paragraph_at_twelve_themes() -> None:
    themes = tuple(f"theme-{index:02d}" for index in range(12))
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
            themes=themes,
        ),
    )

    page = signed_in.get(_PAGE).text

    # One wrapped sentence at any set size (R6): one paragraph, not one chip
    # per theme and not a table.
    assert page.count("data-builds-with") == 1
    start = page.index("data-builds-with")
    row = page[start : page.index("</p>", start)]
    assert all(theme in row for theme in themes)


def test_a_toolchain_manifest_package_json_does_not_carry_omits_the_row(
    tmp_path: Path,
) -> None:
    project = tmp_path / "frontend"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies": ["@slidev/theme-seriph"]}')
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
            toolchain_project=project,
        ),
    )

    page = signed_in.get(_PAGE)

    assert page.status_code == HTTPStatus.OK
    assert "data-builds-with" not in page.text


def test_the_row_uses_the_catalogs_own_separator_between_names() -> None:
    catalog = replace(ENGLISH, deck_theme_separator=" | ")
    lobby = a_lobby(
        catalogs=Catalogs(by_tag={catalog.language_tag: catalog}),
        given=GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
            themes=_THEMES,
        ),
    )
    lobby.sign_in()

    page = lobby.client.get(_PAGE).text

    assert " | ".join(_THEMES) in page
    assert ", ".join(_THEMES) not in page


def test_a_built_decks_page_says_how_long_ago_its_talk_was_built() -> None:
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
        ),
    )

    page = signed_in.get(_PAGE).text

    assert ENGLISH.deck_built.format(age=_HOW_LONG_AGO) in page
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
        GivenDecks(
            store=a_deck_store(built=built),
            source=a_configured_source(_ADDRESS),
        ),
    )

    missing = signed_in.get(_PDF)

    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert ENGLISH.deck_unknown_title in missing.text
    assert missing.headers["content-type"].startswith("text/html")
    assert '"detail"' not in missing.text


@pytest.mark.parametrize(
    "slug",
    ["hello\\deck", 'hello"deck', "hello\ndeck"],
    ids=["a separator", "a quote", "a line break"],
)
def test_a_slug_that_could_reach_a_header_is_refused_before_one_is_built(
    slug: str,
) -> None:
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(slug=slug, built=a_build()),
            source=a_configured_source(_ADDRESS),
        ),
    )

    refused = signed_in.get(f"/deck/{quote(slug, safe='')}/pdf")

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert "content-disposition" not in refused.headers
    assert ENGLISH.deck_unknown_title in refused.text


def test_a_deck_nothing_has_been_built_from_says_so_and_offers_no_view(
    unbuilt_lobby: TestClient,
) -> None:
    page = unbuilt_lobby.get(_PAGE)

    assert page.status_code == HTTPStatus.OK
    assert ENGLISH.deck_not_built_explanation in page.text
    assert ENGLISH.deck_state_never_built in page.text
    assert _PRESENTER not in page.text
    assert _PROJECTOR not in page.text
    assert ENGLISH.deck_pdf not in page.text


@pytest.mark.parametrize(
    "address", [_PROJECTOR, _PRESENTER], ids=["projector", "presenter"]
)
def test_a_view_of_a_deck_nothing_has_been_built_from_answers_nothing(
    unbuilt_talk_host: TestClient,
    address: str,
) -> None:
    assert unbuilt_talk_host.get(address).status_code == HTTPStatus.NOT_FOUND


@pytest.fixture
def talk_host_with_a_removed_build(tmp_path: Path) -> TestClient:
    """A deck whose row still names a build directory that is gone from disk.

    Switching a deck to a newer build does not yet delete the directory an
    older one pointed at, so a row can outlive its directory; the talk it
    would deliver must still answer not-found, never a traceback.
    """
    talk = tmp_path / "talk"
    talk.mkdir()
    (talk / "index.html").write_text("<h1>a talk</h1>", encoding="utf-8")
    client = a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(built=a_build(talk=talk)),
            source=a_configured_source(_ADDRESS),
        ),
    )
    shutil.rmtree(talk)
    return client


@pytest.mark.parametrize(
    "address",
    [_PROJECTOR, _PRESENTER, f"{_PROJECTOR}index.html"],
    ids=["projector", "presenter", "an asset"],
)
def test_a_deck_whose_build_directory_was_removed_answers_nothing(
    talk_host_with_a_removed_build: TestClient,
    address: str,
) -> None:
    assert (
        talk_host_with_a_removed_build.get(address).status_code == HTTPStatus.NOT_FOUND
    )


@pytest.mark.parametrize(
    ("address", "login_redirect"),
    [
        pytest.param(_PAGE, "/login/oidc?next=%2Fdeck%2Fhello-deck", id="page"),
        pytest.param(
            _PROJECTOR, "/login/oidc?next=%2Fdeck%2Fhello-deck%2F", id="projector"
        ),
        pytest.param(
            _PRESENTER,
            "/login/oidc?next=%2Fdeck%2Fhello-deck%2Fpresenter%2F",
            id="presenter",
        ),
        pytest.param(
            f"{_PROJECTOR}index.html",
            "/login/oidc?next=%2Fdeck%2Fhello-deck%2Findex.html",
            id="an asset",
        ),
        pytest.param(_PDF, "/login/oidc?next=%2Fdeck%2Fhello-deck%2Fpdf", id="the pdf"),
    ],
)
def test_a_deck_address_answers_the_login_to_anyone_who_is_not_signed_in(
    address: str,
    login_redirect: str,
) -> None:
    signed_out = a_lobby(
        given=GivenDecks(
            store=a_deck_store(built=a_build()),
            source=a_configured_source(_ADDRESS),
        ),
    )

    refused = signed_out.client.get(address)

    assert refused.status_code == HTTPStatus.FOUND
    assert refused.headers["location"] == login_redirect
    assert _TITLE not in refused.text


def a_secret_a_link_may_point_at(tmp_path: Path) -> Path:
    """A directory holding the secret under every name a build file has."""
    secret = tmp_path / "secret"
    secret.mkdir()
    for name in ("deck.pdf", "app.js", "index.html"):
        (secret / name).write_text(ONLY_THIS_TEST_WROTE_IT, encoding="utf-8")
    return secret


@pytest.mark.parametrize(
    ("replaced", "host", "address"),
    [
        ("deck.pdf", A_BROWSERS_ADDRESS, _PDF),
        ("talk/assets/app.js", A_TALK_ADDRESS, f"{_PROJECTOR}assets/app.js"),
        ("talk/assets", A_TALK_ADDRESS, f"{_PROJECTOR}assets/app.js"),
        ("talk", A_TALK_ADDRESS, f"{_PROJECTOR}assets/app.js"),
        ("talk/index.html", A_TALK_ADDRESS, _PRESENTER),
    ],
    ids=["the pdf", "an asset", "a directory on the way", "the talk", "the page"],
)
def test_a_link_laid_after_the_build_was_adopted_delivers_no_byte_of_its_target(
    tmp_path: Path,
    replaced: str,
    host: str,
    address: str,
) -> None:
    given, run = a_swapped_build(tmp_path, slug=_SLUG)
    signed_in = two_hosts(tmp_path, given=given).signed_in_at(host)
    secret = a_secret_a_link_may_point_at(tmp_path)
    target = run / replaced
    if target.is_dir():
        shutil.rmtree(target)
        target.symlink_to(secret)
    else:
        target.unlink()
        target.symlink_to(secret / target.name)

    refused = signed_in.get(address)

    assert ONLY_THIS_TEST_WROTE_IT not in refused.text
    assert refused.headers["content-type"] != _PDF_TYPE
    if replaced.startswith("talk/assets"):
        assert refused.status_code == HTTPStatus.NOT_FOUND
        assert refused.json() == {"detail": HTTPStatus.NOT_FOUND.phrase}


def test_a_pdf_replaced_by_a_link_answers_as_if_it_were_missing(
    tmp_path: Path,
) -> None:
    given, run = a_swapped_build(tmp_path, slug=_SLUG)
    signed_in = two_hosts(tmp_path, given=given).signed_in_at(A_BROWSERS_ADDRESS)
    (run / "deck.pdf").unlink()
    (run / "deck.pdf").symlink_to(a_secret_a_link_may_point_at(tmp_path) / "deck.pdf")

    refused = signed_in.get(_PDF)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert ENGLISH.deck_unknown_title in refused.text


def test_an_address_no_deck_carries_answers_the_lobbys_own_page(
    lobby: TestClient,
) -> None:
    unknown = lobby.get("/deck/nothing-was-pushed-here")

    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert ENGLISH.deck_unknown_title in unknown.text
    assert unknown.headers["content-type"].startswith("text/html")
    assert '"detail"' not in unknown.text


def test_a_deck_page_carries_the_header_and_its_person_menu(
    lobby: TestClient,
) -> None:
    page = lobby.get(_PAGE).text

    assert f">{ENGLISH.section_decks}<" in page
    assert ENGLISH.menu_account in page
    assert ENGLISH.menu_theme in page
    assert ENGLISH.log_out in page


def test_a_deck_page_marks_no_section_as_the_current_one(lobby: TestClient) -> None:
    assert "aria-current" not in lobby.get(_PAGE).text


def test_a_deck_removed_by_reconciliation_answers_the_lobbys_not_found(
    tmp_path: Path,
) -> None:
    store = a_deck_store(built=a_build())
    hosts = two_hosts(
        tmp_path,
        given=GivenDecks(store=store, source=a_configured_source(_ADDRESS)),
    )
    signed_in = hosts.signed_in_at(A_BROWSERS_ADDRESS)
    talk_host = hosts.signed_in_at(A_TALK_ADDRESS)

    store.mark_removed_except(present=frozenset(), source_id=SOURCE_ID, at=NOW)
    removed = signed_in.get(_PAGE)
    removed_talk = talk_host.get(_PRESENTER)

    store.mark_removed_except(present=frozenset({_SLUG}), source_id=SOURCE_ID, at=NOW)
    returned = signed_in.get(_PAGE)

    assert removed.status_code == HTTPStatus.NOT_FOUND
    assert ENGLISH.deck_unknown_title in removed.text
    assert returned.status_code == HTTPStatus.OK
    assert ENGLISH.deck_state_ready in returned.text
    assert removed_talk.status_code == HTTPStatus.NOT_FOUND
    assert talk_host.get(_PRESENTER).status_code == HTTPStatus.OK


def test_a_deck_whose_build_failed_names_the_attempt_and_what_broke() -> None:
    page = a_page_of_a_deck(built=a_build(), attempt=a_failed_attempt())

    assert ENGLISH.deck_state_failed in page
    assert ENGLISH.deck_failed_title in page
    assert ENGLISH.deck_attempt_label in page
    assert _SHORT_ATTEMPT_COMMIT in page
    assert _ATTEMPT_COMMIT not in page
    assert _HOW_LONG_SINCE_THE_ATTEMPT in page
    assert _WHAT_THE_TOOLCHAIN_SAID in page


# C13.
def test_the_last_good_talk_still_opens_from_a_failed_decks_page(
    tmp_path: Path,
) -> None:
    hosts = two_hosts(
        tmp_path,
        given=GivenDecks(
            store=a_deck_store(built=a_build(), attempt=a_failed_attempt()),
            source=a_configured_source(_ADDRESS),
        ),
    )
    lobby = hosts.signed_in_at(A_BROWSERS_ADDRESS)
    talk_host = hosts.signed_in_at(A_TALK_ADDRESS)

    page = lobby.get(_PAGE).text

    assert ENGLISH.deck_failed_last_talk_stands in page
    assert f'href="{A_TALK_ADDRESS}{_PRESENTER}"' in page
    assert f'href="{A_TALK_ADDRESS}{_PROJECTOR}"' in page
    assert f'href="{_PDF}"' in page
    assert talk_host.get(_PRESENTER).status_code == HTTPStatus.OK
    assert talk_host.get(_PROJECTOR).status_code == HTTPStatus.OK
    assert lobby.get(_PDF).status_code == HTTPStatus.OK


def test_what_a_failing_deck_printed_reaches_the_page_as_words_not_markup() -> None:
    page = a_page_of_a_deck(
        built=a_build(),
        attempt=a_failed_attempt(said=_A_DECK_THAT_WRITES_MARKUP),
    )

    assert _A_DECK_THAT_WRITES_MARKUP not in page
    assert "&lt;script&gt;" in page


def test_a_failed_build_that_said_nothing_is_still_explained() -> None:
    page = a_page_of_a_deck(built=a_build(), attempt=a_failed_attempt(said=None))

    assert ENGLISH.deck_failed_without_a_message in page
    assert "<pre>" not in page


def test_a_deck_that_never_built_and_failed_promises_no_talk_to_open() -> None:
    page = a_page_of_a_deck(attempt=a_failed_attempt())

    assert ENGLISH.deck_state_failed in page
    assert ENGLISH.deck_not_built_explanation in page
    assert ENGLISH.deck_failed_last_talk_stands not in page
    assert _PRESENTER not in page
    assert ENGLISH.deck_pdf not in page


# C13.
def test_a_deck_being_built_locks_both_views_and_still_offers_the_pdf() -> None:
    lobby = a_signed_in_lobby(
        GivenDecks(
            store=a_deck_store(built=a_build(), attempt=a_running_attempt()),
            source=a_configured_source(_ADDRESS),
        ),
    )

    page = lobby.get(_PAGE).text

    assert ENGLISH.deck_state_building in page
    assert ENGLISH.deck_building_explanation in page
    assert ENGLISH.deck_building_label in page
    assert _SHORT_ATTEMPT_COMMIT in page
    assert ENGLISH.deck_building_for.format(age=_HOW_LONG_IT_HAS_RUN) in page
    assert f'href="{A_TALK_ADDRESS}{_PRESENTER}"' not in page
    assert f'href="{A_TALK_ADDRESS}{_PROJECTOR}"' not in page
    assert f"data-locked>{ENGLISH.deck_presenter_view}<" in page
    assert f"data-locked>{ENGLISH.deck_projector_view}<" in page
    assert f'href="{_PDF}"' in page
    assert lobby.get(_PDF).status_code == HTTPStatus.OK


def test_a_deck_that_delivers_a_talk_and_ran_nothing_since_carries_no_banner() -> None:
    page = a_page_of_a_deck(built=a_build())

    assert ENGLISH.deck_state_ready in page
    assert "data-banner" not in page
