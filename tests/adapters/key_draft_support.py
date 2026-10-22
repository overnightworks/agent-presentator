"""The `source_key_drafts` store over a test database, shared by its test modules."""

from pathlib import Path
from typing import Final

from presentator.adapters.identity import TokenIdentifierFactory
from presentator.adapters.key_drafts import SqliteSourceKeyDrafts
from presentator.adapters.secrets import secret_box

INSTANCE_KEY: Final = "an instance key of thirty-two ch"


def a_key_drafts_store(
    database: Path,
    *,
    instance_key: str = INSTANCE_KEY,
) -> SqliteSourceKeyDrafts:
    """The `source_key_drafts` table over that file."""
    return SqliteSourceKeyDrafts(
        database=database,
        identifiers=TokenIdentifierFactory(),
        box=secret_box(instance_key),
    )
