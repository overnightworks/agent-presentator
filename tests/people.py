"""Who may sign in, written straight into a database file.

Nothing in the product creates a person yet, so the adapter tests that need an
owner or a session holder put the row there themselves.
"""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from presentator.adapters.sqlite import rows
from presentator.contracts.models import User


def seed_user(database: Path, person: User) -> None:
    with rows(database) as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO users (id, username, role) VALUES (?, ?, ?)",
            (person.id, person.username, person.role.value),
        )


def copy_of_a_crashed_password_database(destination: Path) -> Path:
    """A file with the password column committed only in its -wal.

    The copy is taken while the writer is still open, so the crash it stands
    for never reached the final checkpoint.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    original = destination.parent / "original" / destination.name
    original.parent.mkdir()
    with closing(sqlite3.connect(original)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        writer.commit()
        for suffix in ("", "-wal"):
            shutil.copyfile(
                original.with_name(original.name + suffix),
                destination.with_name(destination.name + suffix),
            )
    return destination
