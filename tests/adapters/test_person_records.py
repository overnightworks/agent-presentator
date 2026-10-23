"""The per-person deletes and reads of the stores, over real SQLite."""

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from presentator.adapters.identity import (
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
)
from presentator.adapters.preferences import SqlitePersonPreferencesStore
from presentator.adapters.schema import create_tables
from presentator.contracts.models import Role, User
from presentator.contracts.people import SessionTimes
from presentator.contracts.preferences import PersonPreferences
from tests.adapters.key_draft_support import a_key_drafts_store
from tests.people import seed_user

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_THE_PERSON = User(id="the-person", username="pia", role=Role.USER)
_ANOTHER = User(id="another-person", username="ben", role=Role.USER)
_A_REFRESH_WORD = "a-word-nobody-may-read"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    database = tmp_path / "presentator.sqlite3"
    create_tables(database)
    for person in (_THE_PERSON, _ANOTHER):
        seed_user(database, person)
    return database


def test_deleting_an_account_takes_its_sessions_and_leaves_another_account(
    database: Path,
) -> None:
    """S9 (#293): the account goes with its sessions; no other account is touched."""
    sessions = SqliteSessionRecordStore(database)
    accounts = SqliteSubjectUserStore(database, ProcessWriteLock())
    for person in (_THE_PERSON, _ANOTHER):
        sessions.create(
            person.id,
            now=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            refresh_token=None,
            id_token=SecretStr("an-id-token"),
        )

    accounts.delete_account(_THE_PERSON.id)

    assert accounts.get(_THE_PERSON.id) is None
    assert sessions.sessions_of(_THE_PERSON.id) == ()
    assert accounts.get(_ANOTHER.id) == _ANOTHER
    assert len(sessions.sessions_of(_ANOTHER.id)) == 1


def test_asking_for_an_unknown_account_creates_none(database: Path) -> None:
    """S9 (#293): an unknown subject creates no `users` row."""
    accounts = SqliteSubjectUserStore(database, ProcessWriteLock())

    assert accounts.get("nobody-at-all") is None
    with closing(sqlite3.connect(database)) as connection:
        found = connection.execute(
            "SELECT id FROM users WHERE id = ?", ("nobody-at-all",)
        )
        assert found.fetchall() == []


def test_the_sessions_of_a_person_carry_their_times_and_no_secret(
    database: Path,
) -> None:
    """S9 (#293): sessions are counted by their times and never show the token."""
    sessions = SqliteSessionRecordStore(database)
    sessions.create(
        _THE_PERSON.id,
        now=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        refresh_token=SecretStr(_A_REFRESH_WORD),
        id_token=SecretStr("an-id-token"),
    )

    found = sessions.sessions_of(_THE_PERSON.id)

    assert found == (
        SessionTimes(created_at=_NOW, expires_at=_NOW + timedelta(hours=1)),
    )
    assert _A_REFRESH_WORD not in repr(found)


def test_only_sessions_holding_a_refresh_token_are_counted_as_holding_one(
    database: Path,
) -> None:
    """D5 (#419): a session without a refresh token is not counted as holding one."""
    sessions = SqliteSessionRecordStore(database)
    for token in (SecretStr(_A_REFRESH_WORD), None):
        sessions.create(
            _THE_PERSON.id,
            now=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            refresh_token=token,
            id_token=SecretStr("an-id-token"),
        )

    assert sessions.secrets_held(_THE_PERSON.id).refresh_tokens == 1
    assert sessions.secrets_held(_ANOTHER.id).refresh_tokens == 0


def test_deleting_preferences_removes_only_that_persons_row(database: Path) -> None:
    """S9 (#293): preferences go per person and a repeat deletes zero."""
    preferences = SqlitePersonPreferencesStore(database)
    for person in (_THE_PERSON, _ANOTHER):
        preferences.write(person.id, PersonPreferences(language_tag="de"))

    assert preferences.delete(_THE_PERSON.id) == 1
    assert preferences.delete(_THE_PERSON.id) == 0

    assert preferences.read(_THE_PERSON.id) is None
    assert preferences.read(_ANOTHER.id) == PersonPreferences(language_tag="de")


def test_a_draft_is_counted_and_removed_whether_or_not_its_key_still_opens(
    database: Path,
) -> None:
    """S9 (#293): key drafts are counted and removed though their key is gone."""
    minted = a_key_drafts_store(database, instance_key="the key another instance had")
    minted.mint(_THE_PERSON.id, at=_NOW)
    minted.mint(_ANOTHER.id, at=_NOW)
    drafts = a_key_drafts_store(database)
    assert drafts.unconsumed_for(_THE_PERSON.id, newer_than=_NOW) is None

    assert drafts.count_for_owner(_THE_PERSON.id) == 1
    assert drafts.remove_for_owner(_THE_PERSON.id) == 1

    assert drafts.count_for_owner(_THE_PERSON.id) == 0
    assert drafts.count_for_owner(_ANOTHER.id) == 1


class _LockThatSaysWhenAnotherThreadHasToWait:
    """A re-entrant lock that sets `contended` when a second thread finds it held."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contended = threading.Event()

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()

    def __exit__(self, *_: object) -> None:
        self._lock.release()


def test_deleting_an_account_cannot_split_a_sign_in_that_has_read_the_account(
    database: Path,
) -> None:
    """#418 matrix: an in-flight code exchange leaves no session of a gone account."""
    watched = _LockThatSaysWhenAnotherThreadHasToWait()
    lock = ProcessWriteLock(primitive=watched)
    accounts = SqliteSubjectUserStore(database, lock)
    sessions = SqliteSessionRecordStore(database)
    deletion = threading.Thread(target=accounts.delete_account, args=(_THE_PERSON.id,))

    with lock.hold():
        assert accounts.get(_THE_PERSON.id) is not None
        deletion.start()
        assert watched.contended.wait(timeout=10)
        sessions.create(
            _THE_PERSON.id,
            now=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            refresh_token=None,
            id_token=SecretStr("an-id-token"),
        )
    deletion.join(timeout=10)

    assert accounts.get(_THE_PERSON.id) is None
    assert sessions.sessions_of(_THE_PERSON.id) == ()
