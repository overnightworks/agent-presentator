"""Reads the gettext catalogs into the lobby's words (ADR 0012).

Babel owns the `.po` format, so adding a language stays adding a file.
"""

from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from typing import Final

from babel.dates import format_timedelta
from babel.messages.pofile import read_po

from presentator.contracts.text import Catalogs, LobbyText

CATALOG_DIRECTORY: Final = Path(__file__).parent / "catalogs"

_CATALOG_FILES: Final = "*.po"
_LANGUAGE_FIELD: Final = "language_tag"
# Babel rounds an age up to the next unit at 0.85 of it by default, which would
# call six days a week; the list says what the operator would say.
_NO_ROUNDING_UP: Final = 1.0


class IncompleteCatalogError(ValueError):
    """A catalog that misses its language or a message would render a blank word."""


def load_catalogs(directory: Path) -> Catalogs:
    """Read every catalog file the directory holds, keyed by its language."""
    files = sorted(directory.glob(_CATALOG_FILES))
    return Catalogs(
        by_tag={text.language_tag: text for text in map(load_lobby_text, files)},
    )


def load_lobby_text(catalog_file: Path) -> LobbyText:
    """Read every message the lobby needs, or refuse the catalog."""
    with catalog_file.open(encoding="utf-8") as source:
        catalog = read_po(source)
    if catalog.locale is None:
        message = f"{catalog_file}: the catalog names no language"
        raise IncompleteCatalogError(message)
    translated = {
        str(message.id): str(message.string)
        for message in catalog
        if message.id and message.string
    }
    return LobbyText(
        language_tag=str(catalog.locale),
        **_words(translated, missing_in=catalog_file),
    )


def _words(translated: dict[str, str], *, missing_in: Path) -> dict[str, str]:
    wanted = [
        field.name for field in fields(LobbyText) if field.name != _LANGUAGE_FIELD
    ]
    missing = [name for name in wanted if name not in translated]
    if missing:
        message = f"{missing_in}: no message for {', '.join(missing)}"
        raise IncompleteCatalogError(message)
    return {name: translated[name] for name in wanted}


def age_in_words(age: timedelta, language_tag: str) -> str:
    """Say how long ago something changed, in the catalog's language."""
    return format_timedelta(
        -age,
        granularity="minute",
        threshold=_NO_ROUNDING_UP,
        add_direction=True,
        locale=language_tag,
    )


def duration_in_words(elapsed: timedelta, language_tag: str) -> str:
    """Say how long something has been running, in the catalog's language.

    A build a person is waiting on is measured in seconds, and it is still
    going: the words say a length, never a moment in the past.
    """
    return format_timedelta(
        elapsed,
        granularity="second",
        threshold=_NO_ROUNDING_UP,
        locale=language_tag,
    )
