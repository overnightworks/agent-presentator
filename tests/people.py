"""Who may sign in, written straight into a database file.

Nothing in the product creates a person yet, so the adapter tests that need an
owner or a session holder put the row there themselves.
"""

from pathlib import Path

from presentator.adapters.sqlite import rows
from presentator.contracts.models import User


def seed_user(database: Path, person: User) -> None:
    with rows(database) as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO users (id, username, role) VALUES (?, ?, ?)",
            (person.id, person.username, person.role.value),
        )
