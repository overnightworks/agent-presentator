"""The one way this product opens its SQLite file (ADR 0006).

Every store shares it, so how a connection is opened is decided once.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def rows(database: Path) -> Generator[sqlite3.Cursor]:
    """Hand out a cursor on the database and commit what the caller wrote."""
    # Autocommit, so that the one place that needs a transaction can open an
    # immediate one itself instead of fighting an implicit deferred one.
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        yield connection.cursor()
        connection.commit()
    finally:
        connection.close()


def create_tables(database: Path, schema: str) -> None:
    """Make a schema exist; WAL is set once and stays in the file."""
    with rows(database) as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.executescript(schema)
