"""The one way this product opens its SQLite file (ADR 0006)."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def rows(database: Path) -> Generator[sqlite3.Cursor]:
    """A cursor on the database, committed on the way out."""
    # Autocommit, so that the one place that needs a transaction can open an
    # immediate one itself instead of fighting an implicit deferred one.
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        yield connection.cursor()
        connection.commit()
    finally:
        connection.close()


def apply_schema(database: Path, schema: str) -> None:
    """Make the tables exist; WAL is set once and stays in the file."""
    with rows(database) as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.executescript(schema)
