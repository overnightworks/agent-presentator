"""The one reader of a deck's manifest."""

import pytest

from presentator.adapters.deck_manifest import (
    MalformedManifestError,
    read_manifest,
    titled_by,
)


def test_a_manifest_names_its_title_and_language() -> None:
    manifest = read_manifest(b'title = "Kunden"\nlanguage = "en"\n', folder="talk")

    assert (manifest.title, manifest.language) == ("Kunden", "en")


def test_a_key_the_manifest_leaves_out_or_does_not_word_is_nothing() -> None:
    manifest = read_manifest(b"title = 3\n", folder="talk")

    assert (manifest.title, manifest.language) == (None, None)


@pytest.mark.parametrize(
    "manifest",
    [b"not a manifest at all\n", b"\xff\xfe"],
    ids=["is not TOML", "is not text"],
)
def test_a_manifest_that_cannot_be_read_is_malformed_and_names_its_folder(
    manifest: bytes,
) -> None:
    with pytest.raises(MalformedManifestError, match="talk"):
        read_manifest(manifest, folder="talk")


def test_a_manifest_that_names_no_title_cannot_title_a_listing() -> None:
    with pytest.raises(MalformedManifestError, match="names no title"):
        titled_by(b'language = "en"\n', folder="talk")
