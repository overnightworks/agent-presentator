"""An ordinary file of a built talk, as a browser asks for it and is answered."""

from http import HTTPStatus
from pathlib import Path
from typing import Final
from urllib.parse import quote

import pytest

from tests.api.lobby import a_swapped_build

_SLUG: Final = "hello-deck"
_PROJECTOR: Final = f"/deck/{_SLUG}/"


@pytest.mark.parametrize(
    "slug",
    ["Präsentation", "日本語", "price-€"],
    ids=["latin-1", "outside latin-1", "a currency sign"],
)
def test_the_download_of_a_deck_with_a_unicode_name_is_saved_under_that_name(
    tmp_path: Path,
    slug: str,
) -> None:
    signed_in, _ = a_swapped_build(tmp_path, slug=slug)

    download = signed_in.get(f"/deck/{quote(slug)}/pdf")

    assert download.status_code == HTTPStatus.OK
    assert (
        download.headers["content-disposition"]
        == f"attachment; filename*=utf-8''{quote(slug)}.pdf"
    )


def test_a_file_of_no_known_type_is_delivered_as_bytes(tmp_path: Path) -> None:
    signed_in, run = a_swapped_build(tmp_path, slug=_SLUG)
    (run / "talk" / "assets" / "blob").write_bytes(b"\x00\x01")

    blob = signed_in.get(f"{_PROJECTOR}assets/blob")

    assert blob.headers["content-type"] == "application/octet-stream"


def test_a_range_of_a_file_is_answered_with_only_that_range(tmp_path: Path) -> None:
    signed_in, _ = a_swapped_build(tmp_path, slug=_SLUG)

    part = signed_in.get(f"{_PROJECTOR}assets/app.js", headers={"range": "bytes=0-6"})

    assert part.status_code == HTTPStatus.PARTIAL_CONTENT
    assert part.content == b"console"
    assert part.headers["content-range"] == "bytes 0-6/14"


def test_a_range_beyond_a_file_is_refused(tmp_path: Path) -> None:
    signed_in, _ = a_swapped_build(tmp_path, slug=_SLUG)

    refused = signed_in.get(
        f"{_PROJECTOR}assets/app.js",
        headers={"range": "bytes=99-"},
    )

    assert refused.status_code == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE


def test_an_unchanged_file_is_answered_as_not_modified(tmp_path: Path) -> None:
    signed_in, _ = a_swapped_build(tmp_path, slug=_SLUG)
    address = f"{_PROJECTOR}assets/app.js"
    etag = signed_in.get(address).headers["etag"]

    again = signed_in.get(address, headers={"if-none-match": etag})

    assert again.status_code == HTTPStatus.NOT_MODIFIED
    assert again.content == b""


def test_a_head_request_to_an_asset_answers_its_headers_without_its_body(
    tmp_path: Path,
) -> None:
    address = f"{_PROJECTOR}assets/app.js"
    signed_in, _ = a_swapped_build(tmp_path, slug=_SLUG)
    full = signed_in.get(address)

    head = signed_in.head(address)

    assert head.status_code == full.status_code
    assert head.headers["content-length"] == full.headers["content-length"]
    assert head.headers["content-type"] == full.headers["content-type"]
    assert head.content == b""
