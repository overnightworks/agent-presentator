"""The catalog the lobby speaks, and what happens when it is incomplete."""

from pathlib import Path

import pytest

from presentator.adapters.catalog import (
    ENGLISH_CATALOG,
    IncompleteCatalogError,
    load_lobby_text,
)

_HEADER = 'msgid ""\nmsgstr "Language: en\\n"\n'


def test_the_english_catalog_carries_every_word_the_lobby_needs() -> None:
    text = load_lobby_text(ENGLISH_CATALOG)

    assert text.language_tag == "en"
    assert text.login_submit == "Log in"
    assert text.setup_submit == "Create admin"


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

    with pytest.raises(IncompleteCatalogError, match="no message for log_out"):
        load_lobby_text(catalog)
