"""The catalogs the lobby speaks, and what happens when one is incomplete."""

from pathlib import Path

import pytest

from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    IncompleteCatalogError,
    load_catalogs,
    load_lobby_text,
)
from presentator.contracts.text import DEFAULT_LANGUAGE_TAG, Language

_HEADER = 'msgid ""\nmsgstr "Language: en\\n"\n'
_SHIPPED_CATALOG = CATALOG_DIRECTORY / f"{DEFAULT_LANGUAGE_TAG}.po"


def a_catalog_of_another_language(directory: Path) -> None:
    """Adding a language is adding a file: the shipped words under another tag."""
    translated = (
        _SHIPPED_CATALOG.read_text(encoding="utf-8")
        .replace('"Language: en\\n"', '"Language: de\\n"')
        .replace('msgstr "English"', 'msgstr "Deutsch"')
    )
    (directory / "de.po").write_text(translated, encoding="utf-8")


def test_the_shipped_catalog_carries_every_word_the_lobby_needs() -> None:
    text = load_lobby_text(_SHIPPED_CATALOG)

    assert text.language_tag == DEFAULT_LANGUAGE_TAG
    assert text.login_submit == "Log in"
    assert text.setup_submit == "Create admin"


def test_the_instance_offers_the_language_it_ships_with() -> None:
    assert load_catalogs(CATALOG_DIRECTORY).languages() == (
        Language(tag=DEFAULT_LANGUAGE_TAG, name="English"),
    )


def test_a_catalog_file_dropped_into_the_directory_becomes_a_choice(
    tmp_path: Path,
) -> None:
    (tmp_path / _SHIPPED_CATALOG.name).write_text(
        _SHIPPED_CATALOG.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    a_catalog_of_another_language(tmp_path)

    catalogs = load_catalogs(tmp_path)

    assert catalogs.languages() == (
        Language(tag="de", name="Deutsch"),
        Language(tag=DEFAULT_LANGUAGE_TAG, name="English"),
    )
    assert catalogs.text("de").wordmark == catalogs.text(DEFAULT_LANGUAGE_TAG).wordmark


def test_a_catalog_that_names_no_language_is_refused(tmp_path: Path) -> None:
    catalog = tmp_path / "nowhere.po"
    catalog.write_text('msgid "wordmark"\nmsgstr "Presentator"\n', encoding="utf-8")

    with pytest.raises(IncompleteCatalogError, match="names no language"):
        load_lobby_text(catalog)


def test_a_catalog_that_misses_a_word_is_refused(tmp_path: Path) -> None:
    catalog = tmp_path / "half.po"
    catalog.write_text(
        f'{_HEADER}\nmsgid "wordmark"\nmsgstr "Presentator"\n',
        encoding="utf-8",
    )

    with pytest.raises(IncompleteCatalogError, match="no message for language_name"):
        load_lobby_text(catalog)
