"""The one place the whole database schema is made to exist."""

from pathlib import Path

from presentator.adapters.decks import create_deck_tables
from presentator.adapters.identity import create_identity_tables
from presentator.adapters.preferences import create_preference_tables


def create_tables(database: Path) -> None:
    """Make every table exist; the identity step refuses an old database first."""
    create_identity_tables(database)
    create_preference_tables(database)
    create_deck_tables(database)
