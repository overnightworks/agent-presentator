"""A table that reaches an account must be known to the person erasure."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from tests.adapters.test_person_erasure_sqlite import an_instance_with_both_people

_ACCOUNTS = "users"
_INSERT = re.compile(r'INSERT INTO "?(\w+)"?')


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        )
    ]


def _tables_reaching_accounts(connection: sqlite3.Connection) -> set[str]:
    """Every table that references `users`, directly or through another table."""
    references = {
        table: {
            row[2] for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        for table in _tables(connection)
    }
    reached = {_ACCOUNTS}
    grew = True
    while grew:
        grew = False
        for table, targets in references.items():
            if table not in reached and targets & reached:
                reached.add(table)
                grew = True
    return reached


def test_the_tables_reaching_accounts_are_the_tables_the_erased_person_has_rows_in(
    tmp_path: Path,
) -> None:
    instance = an_instance_with_both_people(tmp_path)

    with closing(sqlite3.connect(instance.database)) as connection:
        reaching = _tables_reaching_accounts(connection)
        inserted = {
            match.group(1)
            for statement in connection.iterdump()
            if (match := _INSERT.match(statement))
        }
        seeded = inserted & set(_tables(connection))

    assert reaching == seeded
