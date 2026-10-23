"""Exporting a person over real SQLite: no table that reaches an account is missed."""

import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from presentator.adapters.identity import SqliteSessionRecordStore
from presentator.contracts.people import KeyDraftExport, PersonExport, PersonKind
from tests.adapters.key_draft_support import a_key_drafts_store
from tests.adapters.person_instance_support import (
    THE_PERSON,
    WHAT_THE_GIT_HOST_EXPECTS,
    Instance,
    an_instance_with_both_people,
    tables_reaching_accounts,
)

_TWO_SESSIONS = 2
_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
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
        PersonKind.ID_TOKEN: 1,
    }
    shown = str(asdict(export))
    seeded_secrets = (
        WHAT_THE_GIT_HOST_EXPECTS,
        repr(b"\x11" * 32),
        "PRIVATE KEY",
        "a-refresh-token",
        "an-id-token",
    )
    assert not [secret for secret in seeded_secrets if secret in shown]


def _another_session(instance: Instance, *, keeps_id_token: bool) -> None:
    SqliteSessionRecordStore(instance.database, keeps_id_token=keeps_id_token).create(
        THE_PERSON.id,
        now=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        refresh_token=None,
        id_token=SecretStr("a-second-id-token"),
    )


def test_two_sessions_holding_id_tokens_are_counted_two_over_real_rows(
    tmp_path: Path,
) -> None:
    """#428 cell 22: two ID-token sessions give `id-token`, `secret`, count 2."""
    instance = an_instance_with_both_people(tmp_path)
    _another_session(instance, keeps_id_token=True)

    export = instance.people.export_person(THE_PERSON.id)

    assert {entry.kind: entry.count for entry in export.withheld}[
        PersonKind.ID_TOKEN
    ] == _TWO_SESSIONS
    assert "a-second-id-token" not in str(asdict(export))


def test_sessions_holding_no_id_token_leave_no_id_token_entry_over_real_rows(
    tmp_path: Path,
) -> None:
    """#428 cell 24: the talk host's NULL-token sessions get no `id-token` entry."""
    instance = an_instance_with_both_people(tmp_path)
    with closing(sqlite3.connect(instance.database)) as connection, connection:
        connection.execute("UPDATE sessions SET id_token = NULL")
    _another_session(instance, keeps_id_token=False)

    export = instance.people.export_person(THE_PERSON.id)

    assert export.data.sessions
    assert PersonKind.ID_TOKEN not in {entry.kind for entry in export.withheld}


def test_a_draft_that_no_longer_opens_is_shown_without_its_key_and_counted(
    tmp_path: Path,
) -> None:
    """D5 (#419): an unreadable draft stays in the data, no key, one private key."""
    instance = an_instance_with_both_people(tmp_path)
    store = a_key_drafts_store(instance.database)
    store.remove_for_owner(THE_PERSON.id)
    a_key_drafts_store(
        instance.database, instance_key="a different instance key of 32ch"
    ).mint(THE_PERSON.id, at=datetime(2026, 1, 15, 9, tzinfo=UTC))

    export = instance.people.export_person(THE_PERSON.id)

    assert export.data.key_draft == KeyDraftExport(public_key=None, created_at=None)
    assert {entry.kind: entry.count for entry in export.withheld}[
        PersonKind.KEY_DRAFT_PRIVATE_KEY
    ] == 1


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
