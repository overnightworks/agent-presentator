"""The lobby a route test drives: the real routes over fakes at every port.

Every route test arranges the same instance, so the wiring the host does
stands here once, with the doubles and the accounts a test hands in.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final, Self

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from httpx2 import Request as BrowserRequest
from httpx2 import Response as BrowserResponse
from pydantic import SecretStr
from webauth.config import WebAuthConfig, installed_web_auth_config
from webauth.cookies import issue_session_cookies
from webauth.jwt_verification import KeySetFetch
from webauth.oidc import (
    OidcRelyingParty,
    TokenEndpointAnswer,
    TokenEndpointError,
    TokenRequest,
    TokenRequestPost,
)
from webauth.ports import (
    AuditSink,
    SessionRecordStore,
    SubjectUserStore,
    UserRecord,
    WriteLock,
)

from gitmirror.model import CredentialReference
from presentator.adapters.audit import LoggingAuditSink
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
from presentator.adapters.identity import ProcessWriteLock
from presentator.adapters.secrets import talk_session_secret
from presentator.api.auth import (
    InstalledAuth,
    LobbyPrivate,
    content_security_policy,
    create_lobby,
)
from presentator.api.copresenter import CoPresenterSurface
from presentator.api.pages import Pages
from presentator.api.talk import create_talk, talk_content_security_policy
from presentator.application.decks import Decks
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
from presentator.host.config import Settings
from presentator.host.main import web_auth_config_for
from presentator.host.oidc import admission_of, end_session_origin, oidc_client_config
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
)

NOW: Final = datetime(2026, 1, 15, 9, tzinfo=UTC)
# What `slidev build` of `examples/hello-deck` writes, `--base /deck/hello-deck/`.
_BUILT_TALK: Final = Path(__file__).parent / "built_talk"
_EXPORTED_PDF: Final = Path(__file__).parent / "exported_deck.pdf"
_BUILT_DECK_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
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
ADMIN: Final = f"id-of-{USERNAME}"
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
# The talk host is a second hostname of the same instance.
A_TALK_ADDRESS: Final = "https://talk.testserver"
A_TALK_SOCKET_ADDRESS: Final = "wss://talk.testserver"
AN_ISSUER: Final = "https://auth.example.test/realms/test"
A_CLIENT_ID: Final = "presentator-test"
A_CLIENT_CREDENTIAL: Final = "the-secret-only-this-test-has"


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


def a_web_auth(clock: FrozenClock) -> WebAuthConfig:
    """The host's library configuration, as the test origin reaches it.

    The library reads its now from the test's clock, the one the session
    fake is seeded from.
    """
    host_config = web_auth_config_for(
        SecretStr("t" * 32),
        trusted_proxies="",
        origin=A_BROWSERS_ADDRESS,
    )
    return replace(host_config, wall_clock=lambda: clock.now().timestamp())


def a_talk_web_auth(clock: FrozenClock) -> WebAuthConfig:
    """The talk host's library configuration, signing under its own derived key."""
    host_config = web_auth_config_for(
        SecretStr(talk_session_secret(a_settings().secret_key.get_secret_value())),
        trusted_proxies="",
        origin=A_TALK_ADDRESS,
    )
    return replace(host_config, wall_clock=lambda: clock.now().timestamp())


def configure_oidc(monkeypatch: pytest.MonkeyPatch, directory: Path) -> Path:
    """Name the issuer, the client, a secret file of a test's own and the talk host.

    The talk host signs in through the same client, so a run that names the
    sign-in names its origin too.
    """
    secret_file = directory / "oidc-client-secret"
    secret_file.write_text(f"{A_CLIENT_CREDENTIAL}\n", encoding="utf-8")
    monkeypatch.setenv("PRESENTATOR_TALK_ORIGIN", A_TALK_ADDRESS)
    monkeypatch.setenv("PRESENTATOR_OIDC_ISSUER", AN_ISSUER)
    monkeypatch.setenv("PRESENTATOR_OIDC_CLIENT_ID", A_CLIENT_ID)
    monkeypatch.setenv("PRESENTATOR_OIDC_CLIENT_SECRET_FILE", str(secret_file))
    return secret_file


def _no_token_endpoint(request: TokenRequest) -> TokenEndpointAnswer:
    message = f"no issuer answers a test: {request.url}"
    raise TokenEndpointError(message)


def _no_key_set(url: str) -> bytes:
    raise AssertionError(url)


def a_settings() -> Settings:
    """The settings of an instance at the test origin, signing in at a fake issuer."""
    return Settings(
        secret_key=SecretStr("t" * 32),
        public_origin=A_BROWSERS_ADDRESS,
        talk_origin=A_TALK_ADDRESS,
        copresenter_socket=Path("/run/copresenter.sock"),
        oidc_issuer=AN_ISSUER,
        oidc_client_id=A_CLIENT_ID,
        oidc_client_secret_file=Path("/run/oidc-client-secret"),
    )


def a_content_security_policy() -> str:
    """The policy of an instance at the test origin, signing out at the fake issuer."""
    client = oidc_client_config(
        a_settings(), SecretStr(A_CLIENT_CREDENTIAL), origin=A_BROWSERS_ADDRESS
    )
    return content_security_policy(end_session_origin(client))


def a_relying_party(
    clock: FrozenClock,
    *,
    post_token_request: TokenRequestPost = _no_token_endpoint,
    fetch_key_set: KeySetFetch = _no_key_set,
    origin: str = A_BROWSERS_ADDRESS,
) -> OidcRelyingParty:
    """The issuer's client, whose transports reach nobody unless a test gives some."""
    return OidcRelyingParty(
        oidc_client_config(a_settings(), SecretStr(A_CLIENT_CREDENTIAL), origin=origin),
        post_token_request=post_token_request,
        fetch_key_set=fetch_key_set,
        wall_clock=lambda: clock.now().timestamp(),
        monotonic_clock=lambda: 0.0,
    )


class NobodySignsIn:
    """The accounts of a lobby no test signs in to: any use is the test's mistake."""

    def get(self, user_id: str) -> UserRecord | None:
        raise AssertionError(user_id)

    def create_for_subject(self, subject: str, username: str, role: str) -> UserRecord:
        raise AssertionError((subject, username, role))

    def set_role(self, user_id: str, role: str) -> UserRecord:
        raise AssertionError((user_id, role))


def an_installed_auth(
    clock: FrozenClock,
    *,
    relying_party: OidcRelyingParty | None = None,
    accounts: SubjectUserStore | None = None,
    lock: WriteLock | None = None,
    audit: AuditSink | None = None,
) -> InstalledAuth:
    """The host's sign-in wiring, each part replaceable by the test that proves it."""
    return InstalledAuth(
        config=a_web_auth(clock),
        relying_party=a_relying_party(clock)
        if relying_party is None
        else relying_party,
        admission=admission_of(a_settings()),
        accounts=NobodySignsIn() if accounts is None else accounts,
        lock=ProcessWriteLock() if lock is None else lock,
        audit=LoggingAuditSink() if audit is None else audit,
        content_security_policy=a_content_security_policy(),
        talk_origin=A_TALK_ADDRESS,
    )


def a_configured_source(url: str) -> Source:
    """The one source an installation carries, owned by its admin."""
    return Source(
        id=SOURCE_ID,
        name="decks",
        url=url,
        ref="main",
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
    accounts = FakeUserStore() if users is None else users
    return FakePeople(
        users=accounts,
        sessions=FakeSessionRecordStore(users=accounts),
        clock=FrozenClock(instant=NOW),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SigningIn:
    """The stores and wiring of a lobby a test signs in to through a fake issuer."""

    sessions: SessionRecordStore
    auth: InstalledAuth
    decks: Decks | None = None


def _carried_by(given: GivenDecks) -> dict[str, tuple[DeckFolder, ...] | None]:
    if given.carried is not None:
        return given.carried
    if given.source is not None:
        return {given.source.id: given.folders}
    return {}


def _run_store_of(given: GivenDecks) -> FakeSourceRunStore:
    run_store = FakeSourceRunStore()
    for run in given.runs:
        run_store.record(run)
    return run_store


def a_decks(given: GivenDecks, clock: FrozenClock) -> Decks:
    """The deck use cases over fakes at every port, as a test arranged them."""
    stored = (
        list(given.sources)
        if given.sources
        else ([] if given.source is None else [given.source])
    )
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
            carried=_carried_by(given),
            failures={} if given.failures is None else given.failures,
            stuck=(
                frozenset({given.source.id})
                if given.stuck_mirror and given.source is not None
                else frozenset()
            ),
        ),
        store=FakeDeckStore() if given.store is None else given.store,
        builder=FakeBuildRunner() if given.builder is None else given.builder,
        source_runs=_run_store_of(given),
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
    return decks


def a_lobby_app(
    *,
    people: FakePeople | None = None,
    catalogs: Catalogs = CATALOGS,
    given: GivenDecks = NO_DECKS,
    signing_in: SigningIn | None = None,
) -> tuple[FastAPI, FrozenClock]:
    """The whole lobby wiring, so a test that needs two clients shares one app."""
    crowd = a_crowd() if people is None else people
    clock = crowd.clock
    decks = (
        a_decks(given, clock)
        if signing_in is None or signing_in.decks is None
        else signing_in.decks
    )
    lobby = create_lobby(
        sessions=crowd.sessions if signing_in is None else signing_in.sessions,
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
        auth=an_installed_auth(clock) if signing_in is None else signing_in.auth,
        private=LobbyPrivate(
            voice=(
                given.voice
                if given.voice is not None
                else VoiceStatusUse(private=UnavailableSpeech())
            ),
        ),
    )
    return lobby, clock


def a_talk_app(
    *,
    people: FakePeople,
    copresenter: CoPresenterSurface | None = None,
) -> FastAPI:
    """The whole talk host over the lobby's people, with the co-presenter it serves."""
    clock = people.clock
    return create_talk(
        sessions=people.sessions,
        decks=a_decks(NO_DECKS, clock),
        auth=replace(
            an_installed_auth(clock),
            config=a_talk_web_auth(clock),
            content_security_policy=talk_content_security_policy(),
        ),
        lobby_origin=A_BROWSERS_ADDRESS,
        copresenter=copresenter,
    )


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
) -> Lobby:
    """The whole lobby, with in-memory stores behind every port."""
    people = a_crowd(users)
    app, _ = a_lobby_app(
        people=people,
        catalogs=catalogs,
        given=replace(given, voice=voice),
    )
    return a_browser_at(
        app,
        people=people,
        base_url=A_BROWSERS_ADDRESS,
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


def a_swapped_build(tmp_path: Path, *, slug: str) -> tuple[GivenDecks, Path]:
    """The deck side with one built deck, and the run directory a link may land in.

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
    given = GivenDecks(
        store=store,
        source=a_configured_source("git@heimserver:decks.git"),
        builder=real_builds_under(tmp_path),
    )
    return given, run


def a_signed_in_lobby(given: GivenDecks = NO_DECKS) -> TestClient:
    """The same lobby with its admin's session open."""
    lobby = a_lobby(given=given)
    lobby.sign_in()
    return lobby.client


def put_a_built_deck(store: FakeDeckStore, *, slug: str, owner_id: str) -> None:
    """A deck of this owner whose built talk is the example talk's real build."""
    store.put(
        Deck(
            slug=slug,
            title="Hello Co-Presenter",
            changed_at=NOW,
            owner_id=owner_id,
            source_id=SOURCE_ID,
            commit=_BUILT_DECK_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    store.put_build(
        slug,
        Build(
            directory=_BUILT_TALK,
            pdf=_EXPORTED_PDF,
            commit=_BUILT_DECK_COMMIT,
            built_at=NOW,
        ),
    )
