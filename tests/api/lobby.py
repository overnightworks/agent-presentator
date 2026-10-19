"""The lobby a route test drives: the real routes over fakes at every port.

Every route test arranges the same instance, so the wiring the host does
stands here once, with the doubles and the accounts a test hands in.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final, Self

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from httpx2 import Request as BrowserRequest
from httpx2 import Response as BrowserResponse
from pydantic import SecretStr
from webauth.config import WebAuthConfig, installed_web_auth_config
from webauth.cookies import issue_session_cookies
from webauth.liveness import IdleWindowLiveness

from gitmirror.model import CredentialReference
from presentator.adapters.builds import (
    ContainerToolchain,
    PackageJsonThemes,
    SlidevBuilds,
)
from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    duration_in_words,
    load_catalogs,
)
from presentator.adapters.decks import SourceMirrors
from presentator.api.auth import (
    InstalledAuth,
    LobbyPrivate,
    create_lobby,
)
from presentator.api.copresenter import CoPresenterSurface
from presentator.api.pages import Pages
from presentator.application.decks import Decks
from presentator.application.identity import IDLE_WINDOW, Identity
from presentator.application.preferences import Preferences
from presentator.application.voice import VoiceStatusUse
from presentator.contracts.decks import (
    Build,
    ConnectionCheckResult,
    Deck,
    DeckFolder,
    Source,
    SourceRun,
    SourceRunFailure,
)
from presentator.contracts.models import Role, User
from presentator.contracts.text import DEFAULT_LANGUAGE_TAG, Catalogs
from presentator.contracts.voice import (
    SampleLanguage,
    VoiceDownloadOutcome,
    VoiceId,
    VoiceLoadOutcome,
    VoiceSnapshot,
    VoiceUnavailableError,
)
from presentator.host.main import web_auth_config
from presentator.ports.decks import BuildRunner
from tests.application.fakes import (
    A_REACHABLE_CHECK,
    DEFAULT_THEME_SET,
    FakeBuildRunner,
    FakeConnectionChecker,
    FakeDeckFolders,
    FakeDeckStore,
    FakeDeployKeyDrafts,
    FakeInstanceSettingsStore,
    FakeLocalMount,
    FakePeople,
    FakePersonPreferencesStore,
    FakeSessionRecordStore,
    FakeSourceRunStore,
    FakeSourceStore,
    FakeToolchainThemes,
    FakeUserStore,
    FrozenClock,
    MatchingLiveness,
)

NOW: Final = datetime(2026, 1, 15, 9, tzinfo=UTC)
# No route test waits for a build, so the bound only has to be longer than the
# ages the tests arrange.
BUILD_BOUND: Final = timedelta(minutes=5)
# What this lobby signs a Check connection fingerprint with; no test reads
# this value, only the fingerprint a real Check response carries.
_FINGERPRINT_KEY: Final = b"what only this test's lobby signs a fingerprint with"
CATALOGS: Final = load_catalogs(CATALOG_DIRECTORY)
ENGLISH: Final = CATALOGS.text(DEFAULT_LANGUAGE_TAG)
USERNAME: Final = "felix"
NEIGHBOUR: Final = "anna"
ADMIN: Final = "the-admin"
SOURCE_ID: Final = "the-source-the-instance-carries"
_A_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
# A real browser always sends both: navigation prefers text/html, and a
# same-site request carries Sec-Fetch-Site since Chrome 76 and Firefox 90. A
# test proving what an absent or a foreign claim does overrides these
# explicitly (issue #105).
A_BROWSERS_HEADERS: Final = {"accept": "text/html", "sec-fetch-site": "same-origin"}
# The lobby's cookies carry `Secure`, and a browser sends a `Secure` cookie back
# only over https, so every browser a test drives reaches the lobby over https.
A_BROWSERS_ADDRESS: Final = "https://testserver"
# Starlette's test client opens a socket at its own `ws://` address unless it
# is given a whole one, so a socket test names this one.
A_BROWSERS_SOCKET_ADDRESS: Final = "wss://testserver"
# What a test naming `cookie_secure=False` drives instead: the plain-http
# origin production's own `cookie_secure` derivation picks for an http
# `public_origin` (`host/main.py`).
AN_INSECURE_ORIGIN: Final = "http://testserver"


class UnavailableSpeech:
    """The omitted test arrangement's private speech port."""

    async def snapshot(self) -> VoiceSnapshot:
        """Make the real use case surface the recoverable service failure."""
        raise VoiceUnavailableError

    async def load(self, voice: VoiceId) -> VoiceLoadOutcome:
        """Make the private mutation unavailable in the default test arrangement."""
        del voice
        raise VoiceUnavailableError

    async def download_qwen(self) -> VoiceDownloadOutcome:
        """Make the omitted test arrangement's download capability unavailable."""
        raise VoiceUnavailableError

    async def sample(self, voice: VoiceId, language: SampleLanguage) -> bytes:
        """Make the omitted test arrangement's sample capability unavailable."""
        del voice, language
        raise VoiceUnavailableError


def hand_over_session_cookies(
    client: TestClient,
    session_id: str,
    config: WebAuthConfig,
) -> None:
    """Put the cookies the library issues for this session into the browser."""
    issued = Response()
    issue_session_cookies(issued, session_id, config)
    client.cookies.extract_cookies(
        BrowserResponse(
            HTTPStatus.OK,
            headers=issued.raw_headers,
            request=BrowserRequest("GET", client.base_url),
        ),
    )


@dataclass(frozen=True, slots=True)
class Lobby:
    """A running lobby, the clock its session hangs on, and the people it knows."""

    app: FastAPI
    client: TestClient
    clock: FrozenClock
    people: FakePeople
    config: WebAuthConfig

    def sign_in(self, *, username: str = USERNAME, role: Role | None = None) -> None:
        """Seed the person and a live session, and give the client its cookies.

        `USERNAME` is the instance's admin; anyone else is an ordinary user
        unless the test names the role.
        """
        person = User(
            id=f"id-of-{username}",
            username=username,
            role=role or (Role.ADMIN if username == USERNAME else Role.USER),
        )
        session_id = self.people.open_session_for(person)
        hand_over_session_cookies(self.client, session_id, self.config)

    def another_browser(self) -> Self:
        """A second browser with its own cookie jar over the same instance."""
        return replace(
            self,
            client=TestClient(
                self.app,
                base_url=str(self.client.base_url),
                follow_redirects=False,
                headers=dict(self.client.headers),
            ),
        )

    def own_origin(self) -> str:
        return str(self.client.base_url).rstrip("/")


def a_web_auth(*, cookie_secure: bool = True) -> WebAuthConfig:
    """The host's library configuration, as the test origin reaches it.

    `cookie_secure` stands in for the production derivation from
    `public_origin`'s scheme: a route test names the origin it wants driven.
    """
    host_config = web_auth_config(
        SecretStr("t" * 32),
        liveness=IdleWindowLiveness(int(IDLE_WINDOW.total_seconds())),
        trusted_proxies="",
        public_origin=A_BROWSERS_ADDRESS if cookie_secure else AN_INSECURE_ORIGIN,
    )
    return replace(host_config, allowed_hosts_exact=frozenset({"testserver"}))


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
    checked: ConnectionCheckResult = A_REACHABLE_CHECK
    themes: tuple[str, ...] | None = DEFAULT_THEME_SET
    # Set only by a test proving the real reader end to end; every other test
    # names `themes` and gets the fake above instead.
    toolchain_project: Path | None = None
    key_drafts: FakeDeployKeyDrafts = field(default_factory=FakeDeployKeyDrafts)
    voice: VoiceStatusUse | None = None
    # Set only by a test proving what the real adapter lets a request read.
    builder: BuildRunner | None = None


NO_DECKS: Final = GivenDecks()


def a_crowd(users: FakeUserStore | None = None) -> FakePeople:
    """Nobody signed in yet, on a clock only the test moves."""
    return FakePeople(
        users=FakeUserStore() if users is None else users,
        sessions=FakeSessionRecordStore(clock=FrozenClock(instant=NOW)),
    )


def a_lobby_app(
    *,
    people: FakePeople | None = None,
    catalogs: Catalogs = CATALOGS,
    given: GivenDecks = NO_DECKS,
    copresenter: CoPresenterSurface | None = None,
    cookie_secure: bool = True,
) -> tuple[FastAPI, FrozenClock]:
    """The whole lobby wiring, so a test that needs two clients shares one app."""
    crowd = a_crowd() if people is None else people
    clock = crowd.clock
    identity = Identity(
        users=crowd.users,
        sessions=crowd.sessions,
        clock=clock,
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
    key_drafts = FakeDeployKeyDrafts(
        # Copied rather than shared: `NO_DECKS` is one module-level constant,
        # so every test starting from it must get its own mutable drafts.
        drafts=dict(given.key_drafts.drafts),
        minted=given.key_drafts.minted,
    )
    decks = Decks(
        sources=FakeSourceStore(
            sources=stored,
            hashes=dict(given.hook_hashes),
            key_drafts=key_drafts,
        ),
        key_drafts=key_drafts,
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
        builder=FakeBuildRunner() if given.builder is None else given.builder,
        source_runs=run_store,
        checker=FakeConnectionChecker(answer=given.checked),
        toolchain_themes=(
            PackageJsonThemes(project=given.toolchain_project)
            if given.toolchain_project is not None
            else FakeToolchainThemes(names_to_return=given.themes)
        ),
        build_bound=BUILD_BOUND,
        fingerprint_key=_FINGERPRINT_KEY,
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
            config=a_web_auth(cookie_secure=cookie_secure),
        ),
        private=LobbyPrivate(
            copresenter=copresenter,
            voice=(
                given.voice
                if given.voice is not None
                else VoiceStatusUse(private=UnavailableSpeech())
            ),
        ),
    )
    return lobby, clock


def a_browser_at(
    app: FastAPI,
    *,
    people: FakePeople,
    base_url: str,
    headers: dict[str, str],
) -> Lobby:
    """A browser at an app built over `people`, with nobody signed in yet."""
    return Lobby(
        app=app,
        client=TestClient(
            app,
            base_url=base_url,
            follow_redirects=False,
            headers=headers,
        ),
        clock=people.clock,
        people=people,
        config=installed_web_auth_config(app),
    )


def a_lobby(
    *,
    users: FakeUserStore | None = None,
    catalogs: Catalogs = CATALOGS,
    given: GivenDecks = NO_DECKS,
    voice: VoiceStatusUse | None = None,
    cookie_secure: bool = True,
) -> Lobby:
    """The whole lobby, with in-memory stores behind every port."""
    people = a_crowd(users)
    app, _ = a_lobby_app(
        people=people,
        catalogs=catalogs,
        given=replace(given, voice=voice),
        cookie_secure=cookie_secure,
    )
    return a_browser_at(
        app,
        people=people,
        base_url=A_BROWSERS_ADDRESS if cookie_secure else AN_INSECURE_ORIGIN,
        headers=A_BROWSERS_HEADERS,
    )


def a_lobby_that_claims_no_origin_of_its_own() -> Lobby:
    """A browser old enough to omit Sec-Fetch-Site, for a test that names its own.

    `a_lobby`'s client already claims `Sec-Fetch-Site: same-origin` so most
    tests never have to; a test proving what an absent or a foreign claim
    does needs a client that makes no claim of its own to be overridden.
    """
    people = a_crowd()
    app, _ = a_lobby_app(people=people)
    return a_browser_at(
        app,
        people=people,
        base_url=A_BROWSERS_ADDRESS,
        headers={"accept": "text/html"},
    )


class _NoCredentials:
    def resolve(self, reference: CredentialReference) -> str | None:
        return None


def real_builds_under(root: Path) -> SlidevBuilds:
    """The real adapter over a real directory; only what it reads is ever used."""
    return SlidevBuilds(
        builds=root,
        toolchain=ContainerToolchain(
            image="unused",
            volume="unused",
            memory="1g",
            disk=None,
            bound=timedelta(minutes=1),
        ),
        mirrors=SourceMirrors(
            directory=root / "mirrors",
            credentials=_NoCredentials(),
            pull_timeout=timedelta(minutes=1),
            local_mount=FakeLocalMount(),
        ),
        output_megabytes=1,
    )


def a_swapped_build(tmp_path: Path, *, slug: str) -> tuple[TestClient, Path]:
    """A signed-in lobby on a built deck, and the run directory a link may land in.

    The deck was adopted with plain files; each test then lays its link, which
    is what a deck's own code can do after the build was held.
    """
    run = tmp_path / "run"
    talk = run / "talk"
    (talk / "assets").mkdir(parents=True)
    (talk / "index.html").write_text("<h1>a talk</h1>", encoding="utf-8")
    (talk / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    pdf = run / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.7 the real one")
    store = FakeDeckStore()
    store.put(
        Deck(
            slug=slug,
            title=slug,
            changed_at=NOW,
            owner_id=ADMIN,
            source_id=SOURCE_ID,
            commit=_A_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    store.put_build(
        slug,
        Build(directory=talk, pdf=pdf, commit=_A_COMMIT, built_at=NOW),
    )
    signed_in = a_signed_in_lobby(
        GivenDecks(
            store=store,
            source=a_configured_source("git@heimserver:decks.git"),
            builder=real_builds_under(tmp_path),
        ),
    )
    return signed_in, run


def a_signed_in_lobby(given: GivenDecks = NO_DECKS) -> TestClient:
    """The same lobby with its admin's session open."""
    lobby = a_lobby(given=given)
    lobby.sign_in()
    return lobby.client
