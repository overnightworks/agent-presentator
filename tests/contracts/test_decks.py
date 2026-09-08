"""The theme names a deck page reads off the toolchain's own manifest (ADR 0014)."""

import pytest

from presentator.contracts.decks import theme_names


def test_theme_packages_are_named_by_what_follows_the_slidev_theme_prefix() -> None:
    manifest = """
    {
      "devDependencies": {
        "@slidev/theme-seriph": "0.25.0",
        "@slidev/cli": "52.19.1"
      },
      "dependencies": {
        "@slidev/theme-apple-basic": "0.25.1"
      }
    }
    """

    assert theme_names(manifest) == ("apple-basic", "default", "seriph")


def test_default_is_named_even_when_the_manifest_does_not_list_it() -> None:
    manifest = '{"devDependencies": {"vue": "3.5.42"}}'

    assert theme_names(manifest) == ("default",)


def test_malformed_json_is_not_a_set_to_guess_from() -> None:
    with pytest.raises(ValueError, match="Expecting value"):
        theme_names("not json at all")
