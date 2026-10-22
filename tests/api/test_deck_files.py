"""An ordinary file of a built talk, as a browser asks for it and is answered."""

from http import HTTPStatus
from pathlib import Path
from typing import Final
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_TALK_ADDRESS,
    ENGLISH,
    GivenDecks,
    a_configured_source,
    a_swapped_build,
    real_builds_under,
)
from tests.api.talk_support import ONLY_THIS_TEST_WROTE_IT, two_hosts
from tests.api.test_deck_page import (
    a_build,
    a_deck_store,
    a_talk_host,
)

_SLUG: Final = "hello-deck"
_PROJECTOR: Final = f"/deck/{_SLUG}/"
_PRESENTER: Final = f"/deck/{_SLUG}/presenter/"
_ADDRESS: Final = "git@heimserver:decks.git"
_BUILT_TALK: Final = Path(__file__).parent / "built_talk"


def _talk_host_on_a_built_deck(tmp_path: Path) -> tuple[TestClient, Path]:
    """A browser signed in at the talk host of a built deck, and its run directory."""
    given, run = a_swapped_build(tmp_path, slug=_SLUG)
    return two_hosts(tmp_path, given=given).signed_in_at(A_TALK_ADDRESS), run


def a_build_next_to_a_secret(tmp_path: Path) -> Path:
    """A build directory with something worth stealing outside and beside it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(ONLY_THIS_TEST_WROTE_IT, encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<h1>a talk</h1>", encoding="utf-8")
    (build / "out").symlink_to(outside)
    return build


@pytest.mark.parametrize(
    "slug",
    ["Präsentation", "日本語", "price-€"],
    ids=["latin-1", "outside latin-1", "a currency sign"],
)
def test_the_download_of_a_deck_with_a_unicode_name_is_saved_under_that_name(
    tmp_path: Path,
    slug: str,
) -> None:
    given, _ = a_swapped_build(tmp_path, slug=slug)
    lobby = two_hosts(tmp_path, given=given).signed_in_at(A_BROWSERS_ADDRESS)

    download = lobby.get(f"/deck/{quote(slug)}/pdf")

    assert download.status_code == HTTPStatus.OK
    assert (
        download.headers["content-disposition"]
        == f"attachment; filename*=utf-8''{quote(slug)}.pdf"
    )


def test_a_file_of_no_known_type_is_delivered_as_bytes(tmp_path: Path) -> None:
    signed_in, run = _talk_host_on_a_built_deck(tmp_path)
    (run / "talk" / "assets" / "blob").write_bytes(b"\x00\x01")

    blob = signed_in.get(f"{_PROJECTOR}assets/blob")

    assert blob.headers["content-type"] == "application/octet-stream"


def test_a_range_of_a_file_is_answered_with_only_that_range(tmp_path: Path) -> None:
    signed_in, _ = _talk_host_on_a_built_deck(tmp_path)

    part = signed_in.get(f"{_PROJECTOR}assets/app.js", headers={"range": "bytes=0-6"})

    assert part.status_code == HTTPStatus.PARTIAL_CONTENT
    assert part.content == b"console"
    assert part.headers["content-range"] == "bytes 0-6/14"


def test_a_range_beyond_a_file_is_refused(tmp_path: Path) -> None:
    signed_in, _ = _talk_host_on_a_built_deck(tmp_path)

    refused = signed_in.get(
        f"{_PROJECTOR}assets/app.js",
        headers={"range": "bytes=99-"},
    )

    assert refused.status_code == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE


def test_an_unchanged_file_is_answered_as_not_modified(tmp_path: Path) -> None:
    signed_in, _ = _talk_host_on_a_built_deck(tmp_path)
    address = f"{_PROJECTOR}assets/app.js"
    etag = signed_in.get(address).headers["etag"]

    again = signed_in.get(address, headers={"if-none-match": etag})

    assert again.status_code == HTTPStatus.NOT_MODIFIED
    assert again.content == b""


def test_a_head_request_to_an_asset_answers_its_headers_without_its_body(
    tmp_path: Path,
) -> None:
    address = f"{_PROJECTOR}assets/app.js"
    signed_in, _ = _talk_host_on_a_built_deck(tmp_path)
    full = signed_in.get(address)

    head = signed_in.head(address)

    assert head.status_code == full.status_code
    assert head.headers["content-length"] == full.headers["content-length"]
    assert head.headers["content-type"] == full.headers["content-type"]
    assert head.content == b""


def _a_built_deck() -> GivenDecks:
    return GivenDecks(
        store=a_deck_store(built=a_build()),
        source=a_configured_source(_ADDRESS),
    )


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
    signed_in = a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(built=a_build(talk=a_build_next_to_a_secret(tmp_path))),
            source=a_configured_source(_ADDRESS),
        ),
    )

    refused = signed_in.get(crafted)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert ONLY_THIS_TEST_WROTE_IT not in refused.text


def test_the_talk_itself_is_still_served_from_inside_that_directory(
    tmp_path: Path,
) -> None:
    signed_in = a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(built=a_build(talk=a_build_next_to_a_secret(tmp_path))),
            source=a_configured_source(_ADDRESS),
        ),
    )

    assert "a talk" in signed_in.get(_PROJECTOR).text


def test_a_nonsense_address_under_a_built_talk_is_the_application(
    tmp_path: Path,
) -> None:
    talk_host = a_talk_host(tmp_path, _a_built_deck())
    application = (_BUILT_TALK / "index.html").read_bytes()
    nonsense = talk_host.get(f"{_PROJECTOR}this-slide-was-never-written")

    assert nonsense.status_code == HTTPStatus.OK
    assert nonsense.content == application
    assert ENGLISH.deck_unknown_title not in nonsense.text


def test_a_talk_refuses_a_method_that_is_not_a_read(tmp_path: Path) -> None:
    talk_host = a_talk_host(tmp_path, _a_built_deck())

    assert talk_host.put(_PRESENTER).status_code == HTTPStatus.METHOD_NOT_ALLOWED


@pytest.mark.parametrize(
    "address",
    [_PROJECTOR, _PRESENTER, f"{_PROJECTOR}a-route-the-build-did-not-write"],
    ids=["projector", "presenter", "a missing route"],
)
def test_a_talk_with_an_application_link_outside_its_build_answers_nothing(
    tmp_path: Path,
    address: str,
) -> None:
    marker = "outside the built talk"
    outside = tmp_path / "outside.html"
    outside.write_text(marker, encoding="utf-8")
    talk = tmp_path / "talk"
    talk.mkdir()
    (talk / "index.html").symlink_to(outside)
    signed_in = a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(built=a_build(talk=talk)),
            source=a_configured_source(_ADDRESS),
            builder=real_builds_under(tmp_path),
        ),
    )

    refused = signed_in.get(address)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert marker not in refused.text


def test_a_missing_path_under_a_talk_with_no_application_answers_nothing(
    tmp_path: Path,
) -> None:
    talk = tmp_path / "talk"
    talk.mkdir()
    (talk / "notes.txt").write_text("not an application", encoding="utf-8")
    signed_in = a_talk_host(
        tmp_path,
        GivenDecks(
            store=a_deck_store(built=a_build(talk=talk)),
            source=a_configured_source(_ADDRESS),
        ),
    )

    missing = signed_in.get(_PRESENTER)

    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert "not an application" not in missing.text
