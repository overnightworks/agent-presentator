"""Two people on fakes, each with every kind of row, shared by the person tests."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from presentator.application.decks import Decks
from presentator.application.people import People
from presentator.contracts.decks import Source
from presentator.contracts.models import Role, User
from presentator.contracts.preferences import PersonPreferences
from tests.application.fakes import (
    FakeDeckFolders,
    FakePersonPreferencesStore,
    FakeSessionRecordStore,
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

THE_PERSON = User(id="the-person", username="pia", role=Role.ADMIN)
ANOTHER = User(id="another-person", username="ben", role=Role.USER)
_A_SHORT_WAIT = timedelta(milliseconds=50)
_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)


def _owned_by(owner: User, name: str) -> Source:
    return replace(a_source(name, identifier=f"{name}-id"), owner_id=owner.id)


@dataclass
class Instance:
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
) -> Instance:
    resolved = DecksFakes() if fakes is None else fakes
    persons_sources = (_owned_by(THE_PERSON, "decks"), _owned_by(THE_PERSON, "talks"))
    anothers_source = _owned_by(ANOTHER, "notes")
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
        users={person.id: person for person in (THE_PERSON, ANOTHER)},
    )
    sessions = FakeSessionRecordStore(users=users)
    clock = FrozenClock(instant=_NOW)
    for person in (THE_PERSON, ANOTHER):
        sessions.create(
            person.id,
            now=clock.now(),
            expires_at=clock.now() + timedelta(hours=1),
            refresh_token=None,
            id_token=SecretStr("an-id-token"),
        )
        resolved.key_drafts.mint(person.id, at=clock.now())
    preferences = FakePersonPreferencesStore(
        chosen={
            person.id: PersonPreferences(language_tag="de")
            for person in (THE_PERSON, ANOTHER)
        },
    )
    return Instance(
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
