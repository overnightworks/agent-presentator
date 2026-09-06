"""The lobby a route test drives: the real routes over fakes at every port."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    load_catalogs,
)
from presentator.api.auth import create_lobby
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.application.preferences import Preferences
from presentator.contracts.decks import DeckFolder, Source
from presentator.contracts.models import Credentials, Role, User
from presentator.contracts.text import DEFAULT_LANGUAGE_TAG, Catalogs
from tests.application.fakes import (
    CountingIdentifierFactory,
    FakeDeckFolders,
    FakeDeckStore,
    FakeInstanceSettingsStore,
    FakeLoginAttemptStore,
    FakePersonPreferencesStore,
    FakeSessionRecordStore,
    FakeSourceStore,
    FakeUserStore,
    FrozenClock,
    MarkingCookieSigner,
    ReversibleHasher,
)

NOW: Final = datetime(2026, 1, 15, 9, tzinfo=UTC)
CATALOGS: Final = load_catalogs(CATALOG_DIRECTORY)
ENGLISH: Final = CATALOGS.text(DEFAULT_LANGUAGE_TAG)
USERNAME: Final = "felix"
NEIGHBOUR: Final = "anna"
TYPED_WORDS: Final = "the words only this test types"


@dataclass(frozen=True, slots=True)
class Lobby:
    """A running lobby and the clock its session hangs on."""

    client: TestClient
    clock: FrozenClock

    def set_up_admin(
        self,
        *,
        password: str = TYPED_WORDS,
        repeated: str = TYPED_WORDS,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self.client.post(
            "/setup",
            data={
                "username": USERNAME,
                "password": password,
                "repeated_password": repeated,
            },
            headers=headers,
        )

    def log_in(
        self,
        *,
        username: str = USERNAME,
        password: str = TYPED_WORDS,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            headers=headers,
        )

    def own_origin(self) -> str:
        return str(self.client.base_url).rstrip("/")


def an_account(username: str, *, role: Role = Role.USER) -> Credentials:
    """One account a test can log into, hashed the way the fake hasher hashes."""
    return Credentials(
        user=User(id=f"id-of-{username}", username=username, role=role),
        password_hash=ReversibleHasher().hash(TYPED_WORDS),
    )


def a_user_store(*people: Credentials) -> FakeUserStore:
    """The accounts an instance already has, without going through first start."""
    return FakeUserStore(
        accounts={person.user.username: person for person in people},
    )


def a_lobby(
    *,
    secure_cookies: bool = False,
    users: FakeUserStore | None = None,
    catalogs: Catalogs = CATALOGS,
    folders: tuple[DeckFolder, ...] = (),
    source: Source | None = None,
) -> Lobby:
    """The whole lobby, with in-memory stores behind every port."""
    clock = FrozenClock(instant=NOW)
    identity = Identity(
        users=FakeUserStore() if users is None else users,
        sessions=FakeSessionRecordStore(),
        attempts=FakeLoginAttemptStore(),
        hasher=ReversibleHasher(),
        clock=clock,
        identifiers=CountingIdentifierFactory(),
        cookies=MarkingCookieSigner(),
    )
    preferences = Preferences(
        instance=FakeInstanceSettingsStore(),
        people=FakePersonPreferencesStore(),
        catalogs=catalogs,
    )
    lobby = create_lobby(
        identity=identity,
        decks=Decks(
            sources=FakeSourceStore(source=source),
            folders=FakeDeckFolders(found=folders),
            store=FakeDeckStore(),
            clock=clock,
        ),
        preferences=preferences,
        age_in_words=age_in_words,
        secure_cookies=secure_cookies,
    )
    return Lobby(client=TestClient(lobby, follow_redirects=False), clock=clock)
