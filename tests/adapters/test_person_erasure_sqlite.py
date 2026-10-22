"""Erasing a person over real SQLite and real mirror and build directories."""

import sqlite3
from contextlib import closing
from pathlib import Path

from presentator.contracts.people import PersonErased
from tests.adapters.person_instance_support import (
    ANOTHER,
    THE_PERSON,
    an_instance_with_both_people,
    owned_by,
    still_there,
)


def test_a_person_with_every_kind_of_row_leaves_none_and_no_mirror_or_build(
    tmp_path: Path,
) -> None:
    """S9 (#293) Done when: no table holds a row of the person, nor mirror or build."""
    instance = an_instance_with_both_people(tmp_path)
    owned = owned_by(instance.database, THE_PERSON)
    assert all(owned.values())

    outcome = instance.people.erase_person(THE_PERSON.id, dry_run=False)

    assert isinstance(outcome, PersonErased)
    assert not any(still_there(instance.database, owned).values())
    assert not any(owned_by(instance.database, THE_PERSON).values())
    assert not instance.mirror_of(THE_PERSON).exists()
    assert not instance.build_of(THE_PERSON).exists()


def test_a_second_person_keeps_every_row_mirror_and_build(tmp_path: Path) -> None:
    """E5 (#418): the erasure takes only the named person's rows."""
    instance = an_instance_with_both_people(tmp_path)
    before = owned_by(instance.database, ANOTHER)

    instance.people.erase_person(THE_PERSON.id, dry_run=False)

    assert owned_by(instance.database, ANOTHER) == before
    assert instance.mirror_of(ANOTHER).exists()
    assert instance.build_of(ANOTHER).exists()


def test_a_dry_run_changes_no_row_and_no_directory(tmp_path: Path) -> None:
    """S9 (#293): the dry run changes nothing."""
    instance = an_instance_with_both_people(tmp_path)
    before = owned_by(instance.database, THE_PERSON)

    instance.people.erase_person(THE_PERSON.id, dry_run=True)

    assert owned_by(instance.database, THE_PERSON) == before
    assert instance.mirror_of(THE_PERSON).exists()
    assert instance.build_of(THE_PERSON).exists()


def test_an_unknown_subject_creates_no_account(tmp_path: Path) -> None:
    """S9 (#293): an unknown subject creates no `users` row."""
    instance = an_instance_with_both_people(tmp_path)

    instance.people.erase_person("nobody-at-all", dry_run=False)
    instance.people.erase_person("nobody-at-all", dry_run=True)

    with closing(sqlite3.connect(instance.database)) as connection:
        accounts = connection.execute("SELECT id FROM users ORDER BY id").fetchall()
    assert accounts == [(ANOTHER.id,), (THE_PERSON.id,)]


def test_the_sole_admin_is_erased_like_anyone_and_a_repeat_is_at_zero(
    tmp_path: Path,
) -> None:
    """people.md: the sole admin goes like anyone; a repeat is idempotent."""
    instance = an_instance_with_both_people(tmp_path)

    first = instance.people.erase_person(THE_PERSON.id, dry_run=False)
    again = instance.people.erase_person(THE_PERSON.id, dry_run=False)

    assert isinstance(first, PersonErased)
    assert isinstance(again, PersonErased)
    assert all(entry.count == 0 for entry in again.kinds)
