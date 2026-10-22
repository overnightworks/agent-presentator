"""Exporting a person over real SQLite: no table that reaches an account is missed."""

import sqlite3
from contextlib import closing
from pathlib import Path

from presentator.contracts.people import PersonExport, PersonKind
from tests.adapters.person_instance_support import (
    THE_PERSON,
    an_instance_with_both_people,
    tables_reaching_accounts,
)

_KIND_OF_TABLE = {
    "users": PersonKind.ACCOUNT,
    "sessions": PersonKind.SESSIONS,
    "person_preferences": PersonKind.PREFERENCES,
    "source_key_drafts": PersonKind.KEY_DRAFT,
    "sources": PersonKind.SOURCES,
    "decks": PersonKind.DECKS,
    "source_runs": PersonKind.RUNS,
}


def _kinds_with_rows(export: PersonExport) -> set[PersonKind]:
    data = export.data
    sources = data.sources
    return {
        kind
        for kind, present in {
            PersonKind.ACCOUNT: data.account is not None,
            PersonKind.SESSIONS: bool(data.sessions),
            PersonKind.PREFERENCES: data.preferences is not None,
            PersonKind.KEY_DRAFT: data.key_draft is not None,
            PersonKind.SOURCES: bool(sources),
            PersonKind.DECKS: any(source.decks for source in sources),
            PersonKind.RUNS: any(source.runs for source in sources),
        }.items()
        if present
    }


def test_every_table_reaching_accounts_is_in_the_data_of_a_person_with_rows(
    tmp_path: Path,
) -> None:
    """S10 (#293) Done when: a table with a `user_id` or `owner_id` left out fails."""
    instance = an_instance_with_both_people(tmp_path)
    with closing(sqlite3.connect(instance.database)) as connection:
        reaching = tables_reaching_accounts(connection)

    export = instance.people.export_person(THE_PERSON.id)

    assert reaching == set(_KIND_OF_TABLE)
    assert _kinds_with_rows(export) == set(_KIND_OF_TABLE.values())


def test_the_secret_columns_are_counted_withheld_over_real_rows(
    tmp_path: Path,
) -> None:
    """S10 (#293): the export holds no secret; each withheld kind has its count."""
    instance = an_instance_with_both_people(tmp_path)

    export = instance.people.export_person(THE_PERSON.id)

    assert {entry.kind: entry.count for entry in export.withheld} == {
        PersonKind.SOURCE_CREDENTIAL: 1,
        PersonKind.HOOK_SECRET_HASH: 1,
        PersonKind.KEY_DRAFT_PRIVATE_KEY: 1,
        PersonKind.REFRESH_TOKEN: 1,
    }
    assert "a-refresh-token" not in str(export)


def test_an_unknown_subject_exports_nothing_and_creates_no_account(
    tmp_path: Path,
) -> None:
    """S10 (#293): an unknown subject creates no `users` row."""
    instance = an_instance_with_both_people(tmp_path)

    export = instance.people.export_person("nobody-at-all")

    assert export.withheld == ()
    assert _kinds_with_rows(export) == set()
    with closing(sqlite3.connect(instance.database)) as connection:
        accounts = connection.execute("SELECT COUNT(*) FROM users").fetchone()
    assert accounts == (2,)
