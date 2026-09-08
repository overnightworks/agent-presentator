"""The lobby a route test drives: the real routes over fakes at every port.

Every route test arranges the same instance, so the wiring the host does
stands here once, with the doubles and the accounts a test hands in.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from webauth.config import WebAuthConfig
from webauth.liveness import IdleWindowLiveness
from webauth.proxies import TrustedProxies
from webauth.rate_limit import SingleProcessRateLimitBackend

from presentator.adapters.builds import PackageJsonThemes
from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    duration_in_words,
    load_catalogs,
)
from presentator.api.auth import SESSION_COOKIE, InstalledAuth, create_lobby
from presentator.api.pages import Pages
from presentator.application.decks import Decks
from presentator.application.identity import (
    FAILURE_WINDOW,
    FAILURES_BEFORE_THROTTLE,
    IDLE_WINDOW,
    Identity,
)
from presentator.application.preferences import Preferences
from presentator.contracts.decks import (
    DeckFolder,
    Source,
    SourceRun,
    SourceRunFailure,
)
from presentator.contracts.models import Account, Role
from presentator.contracts.text import DEFAULT_LANGUAGE_TAG, Catalogs
from tests.application.fakes import (
    DEFAULT_THEME_SET,
    CountingIdentifierFactory,
    FakeBuildRunner,
    FakeDeckFolders,
    FakeDeckStore,
    FakeInstanceSettingsStore,
    FakeLocalMount,
    FakeLoginAttemptStore,
    FakePersonPreferencesStore,
    FakeSessionRecordStore,
    FakeSourceRunStore,
    FakeSourceStore,
    FakeToolchainThemes,
    FakeUserStore,
    FrozenClock,
    MarkingCookieSigner,
    MatchingLiveness,
    ReversibleHasher,
)

NOW: Final = datetime(2026, 1, 15, 9, tzinfo=UTC)
# No route test waits for a build, so the bound only has to be longer than the
# ages the tests arrange.
BUILD_BOUND: Final = timedelta(minutes=5)
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


def an_account(username: str, *, role: Role = Role.USER) -> Account:
    """One account a test can log into, hashed the way the fake hasher hashes."""
    return Account(
        id=f"id-of-{username}",
        username=username,
        role=role,
        password_hash=ReversibleHasher().hash(TYPED_WORDS),
    )


def a_user_store(*people: Account) -> FakeUserStore:
    """The accounts an instance already has, without going through first start."""
    return FakeUserStore(
        accounts={person.username: person for person in people},
    )


def a_web_auth(*, hasher: ReversibleHasher) -> WebAuthConfig:
    """The library configuration a route test runs, over the fake hasher."""
    idle_seconds = int(IDLE_WINDOW.total_seconds())
    failure_seconds = int(FAILURE_WINDOW.total_seconds())
    return WebAuthConfig(
        session_secret=SecretStr("t" * 32),
        trusted_proxies=TrustedProxies(),
        password_hasher=hasher,
        rate_limits=SingleProcessRateLimitBackend(),
        allowed_hosts_exact=frozenset({"testserver"}),
        allowed_hosts_patterns=(),
        session_max_age_seconds=idle_seconds,
        session_liveness=IdleWindowLiveness(idle_seconds),
        login_rate_limit=FAILURES_BEFORE_THROTTLE,
        login_lockout_threshold=FAILURES_BEFORE_THROTTLE,
        login_lockout_window_seconds=failure_seconds,
        login_rate_window_seconds=failure_seconds,
        session_cookie_name=SESSION_COOKIE,
        session_cache=None,
    )


def a_configured_source(url: str) -> Source:
    """The one source an installation carries, owned by its admin."""
    return Source(
        id=SOURCE_ID,
        name="decks",
        url=url,
        ref="main",
        secret_location=None,
        owner_id=ADMIN,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class GivenDecks:
    """What the deck side of an instance carries before a test drives it."""

    folders: tuple[DeckFolder, ...] | None = None
    source: Source | None = None
    sources: tuple[Source, ...] = ()
    store: FakeDeckStore | None = None
    runs: tuple[SourceRun, ...] = ()
    carried: dict[str, tuple[DeckFolder, ...] | None] | None = None
    failures: dict[str, SourceRunFailure] | None = None
    # A source whose mirror this instance refuses to let go of, the way a
    # real one left on a filesystem that refuses the delete would.
    stuck_mirror: bool = False
    hook_hashes: dict[str, bytes] = field(default_factory=dict[str, bytes])
    themes: tuple[str, ...] | None = DEFAULT_THEME_SET
    # Set only by a test proving the real reader end to end; every other test
    # names `themes` and gets the fake above instead.
    toolchain_project: Path | None = None


NO_DECKS: Final = GivenDecks()


def a_lobby_app(
    *,
    secure_cookies: bool = False,
    users: FakeUserStore | None = None,
    catalogs: Catalogs = CATALOGS,
    given: GivenDecks = NO_DECKS,
) -> tuple[FastAPI, FrozenClock]:
    """The whole lobby wiring, so a test that needs two clients shares one app."""
    clock = FrozenClock(instant=NOW)
    hasher = ReversibleHasher()
    identity = Identity(
        users=FakeUserStore() if users is None else users,
        sessions=FakeSessionRecordStore(clock=clock),
        attempts=FakeLoginAttemptStore(clock=clock),
        hasher=hasher,
        clock=clock,
        identifiers=CountingIdentifierFactory(),
        cookies=MarkingCookieSigner(),
        liveness=MatchingLiveness(),
    )
    stored = (
        list(given.sources)
        if given.sources
        else ([] if given.source is None else [given.source])
    )
    run_store = FakeSourceRunStore()
    for run in given.runs:
        run_store.record(run)
    if given.carried is not None:
        carried = given.carried
    elif given.source is not None:
        carried = {given.source.id: given.folders}
    else:
        carried = {}
    decks = Decks(
        sources=FakeSourceStore(sources=stored, hashes=dict(given.hook_hashes)),
        folders=FakeDeckFolders(
            carried=carried,
            failures={} if given.failures is None else given.failures,
            stuck=(
                frozenset({given.source.id})
                if given.stuck_mirror and given.source is not None
                else frozenset()
            ),
        ),
        store=FakeDeckStore() if given.store is None else given.store,
        builder=FakeBuildRunner(),
        source_runs=run_store,
        toolchain_themes=(
            PackageJsonThemes(project=given.toolchain_project)
            if given.toolchain_project is not None
            else FakeToolchainThemes(names_to_return=given.themes)
        ),
        build_bound=BUILD_BOUND,
        clock=clock,
        local_mount=FakeLocalMount(),
    )
    # The list and the deck page read the store only; a test arranges what a
    # poll or the hook would already have taken in before anyone opened a page.
    # A test that named folders has that refresh take them in and build them; a
    # test that arranged the store itself has already said what each deck is,
    # and a refresh would reconcile and build rows it never asked about.
    if given.folders is not None:
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
            duration_in_words=duration_in_words,
        ),
        auth=InstalledAuth(
            config=a_web_auth(hasher=hasher),
            secure_cookies=secure_cookies,
        ),
    )
    return lobby, clock


def a_lobby(
    *,
    secure_cookies: bool = False,
    users: FakeUserStore | None = None,
    catalogs: Catalogs = CATALOGS,
    given: GivenDecks = NO_DECKS,
) -> Lobby:
    """The whole lobby, with in-memory stores behind every port."""
    lobby, clock = a_lobby_app(
        secure_cookies=secure_cookies,
        users=users,
        catalogs=catalogs,
        given=given,
    )
    return Lobby(client=TestClient(lobby, follow_redirects=False), clock=clock)


def a_signed_in_lobby(given: GivenDecks = NO_DECKS) -> TestClient:
    """The same lobby with its first admin created and their session open."""
    lobby = a_lobby(given=given)
    lobby.set_up_admin()
    return lobby.client
