"""A table that reaches an account must be known to the person erasure."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from tests.adapters.test_person_erasure_sqlite import (
    THE_PERSON,
    an_instance_with_both_people,
    owned_row_ids,
    tables,
    tables_reaching_accounts,
)

_INSERT = re.compile(r'INSERT INTO "?(\w+)"?')


def test_the_tables_reaching_accounts_are_the_tables_the_erased_person_has_rows_in(
    tmp_path: Path,
) -> None:
    """#418 Tests: a new table fails until the erasure and the seed know it."""
    instance = an_instance_with_both_people(tmp_path)

    with closing(sqlite3.connect(instance.database)) as connection:
        reaching = tables_reaching_accounts(connection)
        owned = owned_row_ids(connection, THE_PERSON.id)
        inserted = {
            match.group(1)
            for statement in connection.iterdump()
            if (match := _INSERT.match(statement))
        }
        seeded = inserted & set(tables(connection))

    assert reaching == seeded
    assert {table for table, ids in owned.items() if ids} == reaching
