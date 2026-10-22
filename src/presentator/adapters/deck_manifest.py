"""The one reader of a deck folder's `deck.toml`."""

import tomllib
from dataclasses import dataclass
from typing import Final

from presentator.contracts.decks import MANIFEST_FILE

_TITLE_KEY: Final = "title"
_LANGUAGE_KEY: Final = "language"


class MalformedManifestError(ValueError):
    """A manifest that cannot be read, or that names no title."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckManifest:
    """What a manifest names; a key it leaves out or does not word is nothing."""

    title: str | None
    language: str | None


def read_manifest(manifest: bytes, *, folder: str) -> DeckManifest:
    """The title and language a manifest names, or why it cannot be read."""
    try:
        table = tomllib.loads(manifest.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as unreadable:
        message = f"{folder}/{MANIFEST_FILE} is not readable TOML: {unreadable}"
        raise MalformedManifestError(message) from unreadable
    return DeckManifest(
        title=_text(table.get(_TITLE_KEY)),
        language=_text(table.get(_LANGUAGE_KEY)),
    )


def titled_by(manifest: bytes, *, folder: str) -> str:
    """The title a manifest names; a manifest that names none is malformed."""
    title = read_manifest(manifest, folder=folder).title
    if title is None:
        message = f"{folder}/{MANIFEST_FILE} names no title"
        raise MalformedManifestError(message)
    return title


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
