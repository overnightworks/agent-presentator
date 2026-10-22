"""Erasing a person: everything they own goes, nothing of anyone else."""

import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import RLock

import pytest

from presentator.application.decks import Decks
from presentator.application.people import People
from presentator.contracts.decks import Source, SourceRun
from presentator.contracts.models import Role, User
from presentator.contracts.people import (
    KindCount,
    PersonBusy,
    PersonDiskLeftover,
    PersonErased,
    PersonErasure,
    PersonKind,
)
from presentator.contracts.preferences import PersonPreferences
from tests.application.fakes import (
    PATIENCE,
    FakeDeckFolders,
    FakePersonPreferencesStore,
    FakeSessionRecordStore,
    FakeSourceRunStore,
    FakeSourceStore,
    FakeUserStore,
    FrozenClock,
)
from tests.application.test_decks import (
    DecksFakes,
    a_folder,
    a_source,
    decks_over,
    having,
)

_THE_PERSON = User(id="the-person", username="pia", role=Role.ADMIN)
_ANOTHER = User(id="another-person", username="ben", role=Role.USER)
_A_SHORT_WAIT = timedelta(milliseconds=50)
_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)


def _owned_by(owner: User, name: str) -> Source:
    return replace(a_source(name, identifier=f"{name}-id"), owner_id=owner.id)


@dataclass
class _Instance:
    """An instance holding two people, each with every kind of row."""

    people: People
    decks: Decks
    fakes: DecksFakes
    users: FakeUserStore
    sessions: FakeSessionRecordStore
    preferences: FakePersonPreferencesStore
    folders: FakeDeckFolders

    def state(self) -> tuple[object, ...]:
        return (
            dict(self.users.users),
            dict(self.sessions.rows),
            dict(self.preferences.chosen),
            dict(self.fakes.key_drafts.drafts),
            tuple(self.decks.sources.all()),
            dict(self.fakes.store.kept),
            tuple(self.fakes.source_runs.recorded),
            list(self.fakes.builder.removed),
            list(self.folders.forgotten),
        )


def _stocked(
    source_store: FakeSourceStore | None,
    sources: tuple[Source, ...],
) -> FakeSourceStore:
    if source_store is None:
        return having(*sources)
    source_store.sources.extend(sources)
    return source_store


def an_instance(
    *,
    folders: FakeDeckFolders | None = None,
    fakes: DecksFakes | None = None,
    source_store: FakeSourceStore | None = None,
) -> _Instance:
    resolved = DecksFakes() if fakes is None else fakes
    persons_sources = (_owned_by(_THE_PERSON, "decks"), _owned_by(_THE_PERSON, "talks"))
    anothers_source = _owned_by(_ANOTHER, "notes")
    mirror = FakeDeckFolders() if folders is None else folders
    for source in (*persons_sources, anothers_source):
        mirror.carried[source.id] = (a_folder(f"{source.name}-deck"),)
    decks = decks_over(
        mirror=mirror,
        sources=_stocked(source_store, (*persons_sources, anothers_source)),
        fakes=resolved,
    )
    decks.refresh()
    users = FakeUserStore(
        users={person.id: person for person in (_THE_PERSON, _ANOTHER)},
    )
    sessions = FakeSessionRecordStore(users=users)
    clock = FrozenClock(instant=_NOW)
    for person in (_THE_PERSON, _ANOTHER):
        sessions.create(
            person.id,
            now=clock.now(),
            expires_at=clock.now() + timedelta(hours=1),
            refresh_token=None,
        )
        resolved.key_drafts.mint(person.id, at=clock.now())
    preferences = FakePersonPreferencesStore(
        chosen={
            person.id: PersonPreferences(language_tag="de")
            for person in (_THE_PERSON, _ANOTHER)
        },
    )
    return _Instance(
        people=People(
            decks=decks,
            accounts=users,
            sessions=sessions,
            preferences=preferences,
            key_drafts=resolved.key_drafts,
            lock_wait=_A_SHORT_WAIT,
        ),
        decks=decks,
        fakes=resolved,
        users=users,
        sessions=sessions,
        preferences=preferences,
        folders=mirror,
    )


def counts(outcome: PersonErasure) -> dict[PersonKind, int]:
    assert isinstance(outcome, PersonErased)
    return {entry.kind: entry.count for entry in outcome.kinds}


@contextmanager
def another_thread_holding(lock: RLock) -> Generator[None]:
    """A refresh under way: some other thread holds the one lock until released."""
    acquired = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock:
            acquired.set()
            release.wait(PATIENCE.total_seconds())

    thread = threading.Thread(target=hold)
    thread.start()
    acquired.wait(PATIENCE.total_seconds())
    try:
        yield
    finally:
        release.set()
        thread.join()


_EVERY_KIND_OF_THE_PERSON = {
    PersonKind.ACCOUNT: 1,
    PersonKind.SESSIONS: 1,
    PersonKind.PREFERENCES: 1,
    PersonKind.KEY_DRAFT: 1,
    PersonKind.SOURCES: 2,
    PersonKind.DECKS: 2,
    PersonKind.RUNS: 2,
    PersonKind.MIRRORS: 2,
    PersonKind.BUILDS: 2,
}
_NOTHING = dict.fromkeys(_EVERY_KIND_OF_THE_PERSON, 0)


def test_a_real_run_removes_every_kind_of_row_and_reports_what_went() -> None:
    """S9 (#293): a real run removes every kind of row and reports each kind's count."""
    instance = an_instance()

    outcome = instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert counts(outcome) == _EVERY_KIND_OF_THE_PERSON
    assert _THE_PERSON.id not in instance.users.users
    assert instance.sessions.sessions_of(_THE_PERSON.id) == ()
    assert instance.preferences.read(_THE_PERSON.id) is None
    assert instance.fakes.key_drafts.count_for_owner(_THE_PERSON.id) == 0
    assert [source.owner_id for source in instance.decks.sources.all()] == [
        _ANOTHER.id,
    ]
    assert [deck.owner_id for deck in instance.fakes.store.kept.values()] == [
        _ANOTHER.id,
    ]
    assert {run.source_id for run in instance.fakes.source_runs.recorded} == {
        "notes-id",
    }
    assert instance.folders.forgotten == ["decks-id", "talks-id"]
    assert (
        len(instance.fakes.builder.removed)
        == _EVERY_KIND_OF_THE_PERSON[PersonKind.BUILDS]
    )


def test_a_real_run_leaves_a_second_persons_rows_alone() -> None:
    """E5 (#418): the erasure takes only the named person's rows."""
    instance = an_instance()
    anothers_before = (
        instance.users.users[_ANOTHER.id],
        instance.preferences.read(_ANOTHER.id),
        instance.fakes.key_drafts.count_for_owner(_ANOTHER.id),
        instance.sessions.sessions_of(_ANOTHER.id),
    )

    instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert anothers_before == (
        instance.users.users[_ANOTHER.id],
        instance.preferences.read(_ANOTHER.id),
        instance.fakes.key_drafts.count_for_owner(_ANOTHER.id),
        instance.sessions.sessions_of(_ANOTHER.id),
    )
    assert "notes-id" not in instance.folders.forgotten


def test_a_dry_run_reports_the_real_runs_counts_and_changes_nothing() -> None:
    """S9 (#293): the dry run lists the real run's counts and changes nothing."""
    instance = an_instance()
    before = instance.state()

    dry = instance.people.erase_person(_THE_PERSON.id, dry_run=True)

    assert instance.state() == before
    assert counts(dry) == _EVERY_KIND_OF_THE_PERSON
    real = instance.people.erase_person(_THE_PERSON.id, dry_run=False)
    assert real == dry


def test_a_second_run_finds_nothing_left_to_remove() -> None:
    """people.md: a repeated erasure is idempotent and answers zero."""
    instance = an_instance()
    instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    again = instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert counts(again) == _NOTHING


def test_an_unknown_subject_answers_zero_and_creates_nothing() -> None:
    """S9 (#293): an unknown subject answers zero counts and creates no row."""
    instance = an_instance()
    before = instance.state()

    dry = instance.people.erase_person("nobody-at-all", dry_run=True)
    real = instance.people.erase_person("nobody-at-all", dry_run=False)

    assert counts(dry) == _NOTHING
    assert counts(real) == _NOTHING
    assert instance.state() == before


def test_a_person_owning_no_source_is_erased_without_waiting_for_a_refresh() -> None:
    """S9 (#418 order): a person without a source has no lock to take."""
    instance = an_instance()
    sourceless = User(id="sourceless", username="sam", role=Role.USER)
    instance.users.users[sourceless.id] = sourceless

    with another_thread_holding(instance.decks.one_at_a_time):
        dry = instance.people.erase_person(sourceless.id, dry_run=True)
        real = instance.people.erase_person(sourceless.id, dry_run=False)

    assert counts(dry) == {**_NOTHING, PersonKind.ACCOUNT: 1}
    assert real == dry
    assert sourceless.id not in instance.users.users


def test_a_refresh_under_way_makes_a_real_run_answer_busy_and_delete_nothing() -> None:
    """S9 (#293): a refresh under way means busy and nothing deleted."""
    instance = an_instance()
    before = instance.state()

    with another_thread_holding(instance.decks.one_at_a_time):
        outcome = instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert outcome == PersonBusy(
        running=(KindCount(kind=PersonKind.RUNNING_WORK, count=1),),
    )
    assert instance.state() == before


def test_a_dry_run_lists_a_refresh_under_way_as_running_work() -> None:
    """S9 (#293): a dry run never waits and lists a running refresh as running work."""
    instance = an_instance()

    with another_thread_holding(instance.decks.one_at_a_time):
        outcome = instance.people.erase_person(_THE_PERSON.id, dry_run=True)

    assert counts(outcome) == {
        **_EVERY_KIND_OF_THE_PERSON,
        PersonKind.RUNNING_WORK: 1,
    }


def test_an_unknown_subjects_dry_run_does_not_list_running_work() -> None:
    """S9 (#293): an unknown subject owns nothing, so no running work is listed."""
    instance = an_instance()

    with another_thread_holding(instance.decks.one_at_a_time):
        outcome = instance.people.erase_person("nobody-at-all", dry_run=True)

    assert counts(outcome) == _NOTHING


def test_a_mirror_that_cannot_go_stops_the_run_with_the_person_still_there() -> None:
    """E5 (#418): a mirror that cannot go stops the run with the account kept."""
    stuck = FakeDeckFolders(stuck=frozenset({"talks-id"}))
    instance = an_instance(folders=stuck)

    outcome = instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert outcome == PersonDiskLeftover(source="talks")
    assert _THE_PERSON.id in instance.users.users
    assert "talks-id" in {source.id for source in instance.decks.sources.all()}
    assert any(
        deck.source_id == "talks-id" for deck in instance.fakes.store.kept.values()
    )


def test_a_repeat_after_a_leftover_finishes_the_erasure() -> None:
    """E5 (#418): a repeat after a disk leftover finishes the erasure."""
    stuck = FakeDeckFolders(stuck=frozenset({"talks-id"}))
    instance = an_instance(folders=stuck)
    instance.people.erase_person(_THE_PERSON.id, dry_run=False)
    stuck.stuck = frozenset()

    outcome = instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert isinstance(outcome, PersonErased)
    assert _THE_PERSON.id not in instance.users.users
    assert [source.owner_id for source in instance.decks.sources.all()] == [
        _ANOTHER.id,
    ]


@dataclass
class _FoldersThatWatchTheRows(FakeDeckFolders):
    """Records what still stood in the database when a mirror was removed."""

    watch: Callable[[], tuple[object, ...]] = field(default=lambda: ())
    seen: list[tuple[object, ...]] = field(default_factory=list[tuple[object, ...]])

    def forget(self, source: Source) -> bool:
        self.seen.append(self.watch())
        return super().forget(source)


def test_files_go_before_the_rows_that_name_them() -> None:
    """S9 (#293): files before rows."""
    folders = _FoldersThatWatchTheRows()
    instance = an_instance(folders=folders)
    folders.watch = lambda: (
        _THE_PERSON.id in instance.users.users,
        len(instance.fakes.store.for_source("decks-id")),
        len(instance.decks.sources.all()),
    )

    instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert folders.seen[0] == (True, 1, 3)


@dataclass
class _RunsThatGrowWhenRead(FakeSourceRunStore):
    """A count that moves between the read that confirmed it and the removal."""

    def recent(self, source_id: str) -> tuple[SourceRun, ...]:
        found = super().recent(source_id)
        self.recorded.append(self.recorded[0])
        return found


def test_a_count_that_moves_under_the_hold_fails_loud_instead_of_skipping() -> None:
    """#418 order: a count that moves under the hold fails loud, never skips."""
    runs = _RunsThatGrowWhenRead()
    instance = an_instance(fakes=DecksFakes(source_runs=runs))

    with pytest.raises(RuntimeError, match="changed under the refresh lock"):
        instance.people.erase_person(_THE_PERSON.id, dry_run=False)

    assert _THE_PERSON.id in instance.users.users


@dataclass
class _SourcesThatVanishAfterTheFirstListing(FakeSourceStore):
    """A source removed by someone else between the listing and the count."""

    listings_left: int = 1000

    def all(self) -> tuple[Source, ...]:
        if self.listings_left == 0:
            return ()
        self.listings_left -= 1
        return super().all()


def test_a_source_that_vanishes_before_it_is_counted_fails_loud() -> None:
    """#418 order: a source that vanishes between listing and counting fails loud."""
    sources = _SourcesThatVanishAfterTheFirstListing()
    instance = an_instance(source_store=sources)
    sources.listings_left = 1

    with pytest.raises(RuntimeError, match="vanished while the person was counted"):
        instance.people.erase_person(_THE_PERSON.id, dry_run=True)
