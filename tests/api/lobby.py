"""The lobby a route test drives: the real routes over fakes at every port.

Every route test arranges the same instance, so the wiring the host does
stands here once, with the doubles and the accounts a test hands in.
"""

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
from presentator.api.pages import Pages
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.application.preferences import Preferences
from presentator.contracts.decks import DeckFolder, Source
from presentator.contracts.models import Credentials, Role, User
from presentator.contracts.text import DEFAULT_LANGUAGE_TAG, Catalogs
from tests.application.fakes import (
    CountingIdentifierFactory,
    FakeBuildRunner,
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
ADMIN: Final = "the-admin"
SOURCE_ID: Final = "the-source-the-instance-carries"
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


def a_configured_source(url: str) -> Source:
    """The one source an installation carries, owned by its admin."""
    return Source(
        id=SOURCE_ID,
        name="decks",
        url=url,
        ref="main",
        credential_reference=None,
        owner_id=ADMIN,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class GivenDecks:
    """What the deck side of an instance carries before a test drives it."""

    folders: tuple[DeckFolder, ...] | None = None
    source: Source | None = None
    store: FakeDeckStore | None = None


NO_DECKS: Final = GivenDecks()


def a_lobby(
    *,
    secure_cookies: bool = False,
    users: FakeUserStore | None = None,
    catalogs: Catalogs = CATALOGS,
    given: GivenDecks = NO_DECKS,
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
    decks = Decks(
        sources=FakeSourceStore(
            sources=[] if given.source is None else [given.source],
        ),
        folders=FakeDeckFolders(
            carried={} if given.source is None else {given.source.id: given.folders},
        ),
        store=FakeDeckStore() if given.store is None else given.store,
        # Nothing builds at this layer: a test arranges the build its deck
        # delivers from, the way it arranges the row.
        builder=FakeBuildRunner(fails=True),
        clock=clock,
    )
    # The list and the deck page read the store only; a test arranges what a
    # poll or the hook would already have taken in before anyone opened a page.
    # No folders named is not the source carrying none: it is a test that
    # populated the store itself, so the one refresh here must read nothing
    # rather than reconcile away what the test already put there.
    decks.refresh()
    lobby = create_lobby(
        identity=identity,
        decks=decks,
        pages=Pages(
            preferences=Preferences(
                instance=FakeInstanceSettingsStore(),
                people=FakePersonPreferencesStore(),
                catalogs=catalogs,
            ),
            age_in_words=age_in_words,
        ),
        secure_cookies=secure_cookies,
        fetch_hook=None,
    )
    return Lobby(client=TestClient(lobby, follow_redirects=False), clock=clock)


def a_signed_in_lobby(given: GivenDecks = NO_DECKS) -> TestClient:
    """The same lobby with its first admin created and their session open."""
    lobby = a_lobby(given=given)
    lobby.set_up_admin()
    return lobby.client
