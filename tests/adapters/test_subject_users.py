"""The accounts an identity provider vouches for, in the users table."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from webauth.ports import UsernameTakenError

from presentator.adapters.identity import SqliteSubjectUserStore, create_identity_tables
from presentator.contracts.models import Role


@pytest.fixture
def database(tmp_path: Path) -> Path:
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    return database


@pytest.fixture
def accounts(database: Path) -> SqliteSubjectUserStore:
    return SqliteSubjectUserStore(database)


def test_an_account_created_for_a_subject_is_found_by_that_subject(
    accounts: SqliteSubjectUserStore,
) -> None:
    created = accounts.create_for_subject("subject-1", "felix", "admin")

    found = accounts.get("subject-1")

    assert found is not None
    assert (found.id, found.username, found.role) == ("subject-1", "felix", "admin")
    assert created.id == found.id


def test_a_subject_that_differs_only_in_case_is_another_account(
    accounts: SqliteSubjectUserStore,
) -> None:
    accounts.create_for_subject("Subject-1", "felix", "user")

    assert accounts.get("subject-1") is None
    assert accounts.get("Subject-1 ") is None


def test_a_username_another_account_holds_is_refused_as_taken(
    accounts: SqliteSubjectUserStore,
) -> None:
    accounts.create_for_subject("subject-1", "felix", "user")

    with pytest.raises(UsernameTakenError):
        accounts.create_for_subject("subject-2", "felix", "user")

    assert accounts.get("subject-2") is None


def test_a_subject_that_already_has_an_account_is_not_a_taken_username(
    accounts: SqliteSubjectUserStore,
) -> None:
    accounts.create_for_subject("subject-1", "felix", "user")

    with pytest.raises(sqlite3.IntegrityError) as refused:
        accounts.create_for_subject("subject-1", "anna", "user")

    assert not isinstance(refused.value, UsernameTakenError)


def test_a_changed_role_is_stored_and_returned(
    accounts: SqliteSubjectUserStore,
) -> None:
    accounts.create_for_subject("subject-1", "felix", "user")

    changed = accounts.set_role("subject-1", Role.ADMIN.value)

    assert changed.role == Role.ADMIN
    stored = accounts.get("subject-1")
    assert stored is not None
    assert stored.role == Role.ADMIN


def test_a_role_for_an_account_that_does_not_exist_raises_and_writes_nothing(
    accounts: SqliteSubjectUserStore,
    database: Path,
) -> None:
    with pytest.raises(LookupError):
        accounts.set_role("nobody", Role.ADMIN.value)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)
