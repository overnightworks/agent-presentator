"""Find, by the schema's own foreign keys, every row a person owns."""

import json
import sqlite3

ACCOUNTS = "users"


def tables(connection: sqlite3.Connection) -> list[str]:
    return [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        )
    ]


def tables_reaching_accounts(connection: sqlite3.Connection) -> set[str]:
    """Every table that references `users`, directly or through another table."""
    references = {
        table: {
            row[2] for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        for table in tables(connection)
    }
    reached = {ACCOUNTS}
    grew = True
    while grew:
        grew = False
        for table, targets in references.items():
            if table not in reached and targets & reached:
                reached.add(table)
                grew = True
    return reached


def owned_row_ids(
    connection: sqlite3.Connection, person_id: str
) -> dict[str, set[int]]:
    """Per table that reaches `users`, the rowids the person owns, parents first.

    Taken before an erasure, these name rows that stay findable after their
    parents are gone, so an orphan is still counted.
    """
    reaching = tables_reaching_accounts(connection)
    owned: dict[str, set[int]] = {table: set() for table in reaching}
    owned[ACCOUNTS] = {
        row[0]
        for row in connection.execute(
            "SELECT rowid FROM users WHERE id = ?",
            (person_id,),
        )
    }
    grew = True
    while grew:
        grew = False
        for table in reaching - {ACCOUNTS}:
            for foreign_key in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall():
                parent = foreign_key[2]
                if parent not in reaching:
                    continue
                found = _rows_following(connection, table, foreign_key, owned[parent])
                if not found <= owned[table]:
                    owned[table] |= found
                    grew = True
    return owned


def rows_still_there(
    connection: sqlite3.Connection, owned: dict[str, set[int]]
) -> dict[str, int]:
    """Per table, how many of the previously owned rowids still exist."""
    return {
        table: connection.execute(
            _COUNT_OF_ROWIDS.format(table=table), (json.dumps(sorted(ids)),)
        ).fetchone()[0]
        for table, ids in owned.items()
    }


_COUNT_OF_ROWIDS = (
    'SELECT COUNT(*) FROM "{table}" WHERE rowid IN (SELECT value FROM json_each(?))'
)
_ROWS_FOLLOWING = (
    'SELECT rowid FROM "{table}" WHERE "{child}" IN'
    ' (SELECT "{key}" FROM "{parent}" WHERE rowid IN (SELECT value FROM json_each(?)))'
)


def _rows_following(
    connection: sqlite3.Connection,
    table: str,
    foreign_key: tuple[int, int, str, str, str],
    parent_rowids: set[int],
) -> set[int]:
    query = _ROWS_FOLLOWING.format(
        table=table, child=foreign_key[3], key=foreign_key[4], parent=foreign_key[2]
    )
    return {
        row[0]
        for row in connection.execute(query, (json.dumps(sorted(parent_rowids)),))
    }
