"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.routing import Host, Router
from webauth.config import SessionLifetime, WebAuthConfig
from webauth.oidc import OidcClientConfig, OidcRelyingParty
from webauth.proxies import TrustedProxies
from webauth.rate_limit import SingleProcessRateLimitBackend

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.adapters.agent_tokens import PersonAccessTokens, WorkloadToken
from presentator.adapters.audit import LoggingAuditSink
from presentator.adapters.build_worker import WorkerToolchain
from presentator.adapters.builds import (
    ContainerToolchain,
    DaemonRefusedError,
    DeckToolchain,
    HostToolchain,
    PackageJsonThemes,
    SlidevBuilds,
    the_daemon_of_this_machine,
)
from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    duration_in_words,
    load_catalogs,
)
from presentator.adapters.deck_texts import MirroredDeckTexts
from presentator.adapters.decks import (
    FilesystemLocalMount,
    MirroredConnectionChecker,
    MirroredDeckFolders,
    SourceCredentials,
    SourceMirrors,
    SqliteDeckStore,
    SqliteSourceRunStore,
    SqliteSourceStore,
)
from presentator.adapters.identity import (
    OldDatabaseError,
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    SystemClock,
    TokenIdentifierFactory,
)
from presentator.adapters.key_drafts import SqliteSourceKeyDrafts
from presentator.adapters.preferences import (
    SqliteInstanceSettingsStore,
    SqlitePersonPreferencesStore,
)
from presentator.adapters.schema import create_tables
from presentator.adapters.secrets import (
    connection_fingerprint_key,
    secret_box,
    talk_session_secret,
)
from presentator.adapters.sentences import split_sentences
from presentator.adapters.speech import UdsSpeaking, UdsSpeech, UdsSpeechHearing
from presentator.api.auth import (
    InstalledAuth,
    LobbyPrivate,
    content_security_policy,
    create_lobby,
)
from presentator.api.copresenter import CoPresenterSurface
from presentator.api.pages import Pages
from presentator.api.people import AdminPeople
from presentator.api.talk import create_talk, talk_content_security_policy
from presentator.application.copresenter import AskLimiter, CoPresenterUse
from presentator.application.decks import Decks
from presentator.application.identity import SESSION_LIFETIME, SESSION_SWEEP_INTERVAL
from presentator.application.people import People
from presentator.application.preferences import Preferences
from presentator.application.voice import VoiceStatusUse
from presentator.host.config import (
    NO_BOUND_AT_ALL,
    Settings,
    WhereBuildsRun,
    cannot_start,
    load_settings,
)
from presentator.host.oidc import (
    admission_of,
    client_configs_of,
    relying_party,
)
from presentator.host.polling import Schedule
from presentator.host.worker_serving import emergency_end

_WITHOUT_A_SANDBOX = (
    "build_image and build_volume say what a deck's build runs in and where it"
    " writes, and a deck is code (line 14a): without both of them there is no"
    " sandbox. Ask for the development run knowingly with"
    " build_runner=host, or name them"
)
_WORKER_NOT_ANSWERING = (
    "The build worker did not answer within {window:g} seconds. Check that the"
    " build-worker service is running and shares the socket directory with this"
    " service."
)
_NOTHING_BOUNDS_A_BUILD = (
    "this machine's %s storage driver does not take a size for a container's own"
    " filesystem, so a build could fill this machine with what it writes beside"
    f" its talk. Set build_disk to {NO_BOUND_AT_ALL} to run without that bound"
    " knowingly"
)
_UNBOUNDED_BY_CHOICE = (
    f"build_disk is {NO_BOUND_AT_ALL}, so nothing but the time a step may take"
    " bounds what one build writes into its own container"
)

_log = logging.getLogger(__name__)

RelyingPartyFor = Callable[[OidcClientConfig], OidcRelyingParty]


@dataclass(frozen=True, slots=True, kw_only=True)
class Instance:
    """What a running instance is: the addresses, and the schedules beside them."""

    lobby: FastAPI
    talk: FastAPI
    poller: Schedule
    session_sweep: Schedule


def build_instance(
    settings: Settings,
    relying_party_for: RelyingPartyFor = relying_party,
) -> Instance:
    """Choose the adapter behind every port and hand the routes their use cases."""
    try:
        create_tables(settings.database)
    except OldDatabaseError as refused:
        raise cannot_start(str(refused)) from None
    identifiers = TokenIdentifierFactory()
    clock = SystemClock()
    # One box for both directions: what the store writes down is what the
    # resolver opens at the pull, and neither picks its own key.
    box = secret_box(settings.secret_key.get_secret_value())
    sources = SqliteSourceStore(
        database=settings.database,
        identifiers=identifiers,
        box=box,
    )
    key_drafts = SqliteSourceKeyDrafts(
        database=settings.database,
        identifiers=identifiers,
        box=box,
    )
    source_timeout = timedelta(seconds=settings.source_timeout_seconds)
    # The one mounted directory a file-kind source's address may resolve
    # under, real filesystem and all: shared by every use, since a symlink or
    # a remount between them must be caught the same way each time.
    local_mount = FilesystemLocalMount(mount=settings.local_sources_mount)
    mirrors = SourceMirrors(
        directory=settings.mirrors,
        credentials=SourceCredentials(database=settings.database, box=box),
        pull_timeout=source_timeout,
        local_mount=local_mount,
    )
    build_bound = timedelta(seconds=settings.build_timeout_seconds)
    decks = Decks(
        sources=sources,
        key_drafts=key_drafts,
        folders=MirroredDeckFolders(mirrors=mirrors),
        store=SqliteDeckStore(database=settings.database),
        builder=SlidevBuilds(
            builds=settings.builds,
            toolchain=_what_builds_a_deck(settings, bound=build_bound),
            mirrors=mirrors,
            output_megabytes=settings.build_output_megabytes,
        ),
        source_runs=SqliteSourceRunStore(database=settings.database),
        # The same bound a scheduled pull takes: Check connection asks the
        # same remote for the same one thing, just without writing it down.
        checker=MirroredConnectionChecker(
            check_timeout=source_timeout,
            local_mount=local_mount,
            known_hosts=settings.mirrors / "known_hosts",
        ),
        toolchain_themes=PackageJsonThemes(project=settings.toolchain),
        # One toolchain step's bound, which is what the refresh needs: it never
        # reads a live build, only what a process that is gone left behind.
        build_bound=build_bound,
        fingerprint_key=connection_fingerprint_key(
            settings.secret_key.get_secret_value(),
        ),
        clock=SystemClock(),
        local_mount=local_mount,
    )
    sessions = SqliteSessionRecordStore(settings.database, keeps_id_token=True)
    # One use case object serves both callers, so the hook and the poll share
    # the one refresh that runs at a time.
    lobby_auth, talk_auth = installed_auths(settings, relying_party_for)
    return Instance(
        lobby=_lobby(settings, decks, sessions, key_drafts, lobby_auth),
        talk=_talk(settings, decks, talk_auth, mirrors),
        poller=Schedule(
            chore=decks.refresh,
            interval=timedelta(seconds=settings.source_poll_seconds),
        ),
        session_sweep=_session_sweep(sessions, clock),
    )


def _lobby(
    settings: Settings,
    decks: Decks,
    sessions: SqliteSessionRecordStore,
    key_drafts: SqliteSourceKeyDrafts,
    auth: InstalledAuth,
) -> FastAPI:
    """The lobby host, with the pages and the private routes it serves."""
    return create_lobby(
        sessions=sessions,
        decks=decks,
        pages=_pages(settings),
        auth=auth,
        private=_lobby_private(settings, decks, sessions, key_drafts, auth),
    )


def _talk(
    settings: Settings,
    decks: Decks,
    auth: InstalledAuth,
    mirrors: SourceMirrors,
) -> FastAPI:
    """The talk host, with the co-presenter it speaks to.

    Its own session store keeps no ID token: the talk host has no logout route.
    """
    sessions = SqliteSessionRecordStore(settings.database)
    return create_talk(
        sessions=sessions,
        decks=decks,
        auth=auth,
        lobby_origin=settings.public_origin,
        copresenter=_copresenter_surface(settings, sessions, decks, auth, mirrors),
    )


def _copresenter_surface(
    settings: Settings,
    sessions: SqliteSessionRecordStore,
    decks: Decks,
    auth: InstalledAuth,
    mirrors: SourceMirrors,
) -> CoPresenterSurface:
    """What only a signed-in admin may reach at the talk host: the agent service."""
    return CoPresenterSurface(
        use=CoPresenterUse(
            answers=ServiceAnswerer(
                origin=settings.agent_service,
                workload_token=WorkloadToken(auth.relying_party),
            ),
            speech=UdsSpeechHearing(settings.speech_socket),
            decks=decks.store,
            sources=decks.sources,
            texts=MirroredDeckTexts(mirrors=mirrors),
            boundaries=split_sentences,
            speaking=UdsSpeaking(settings.speech_socket),
            person_access=PersonAccessTokens(auth.relying_party, sessions, auth.config),
        ),
        limiter=AskLimiter(clock=SystemClock()),
        browser_origin=settings.talk_origin,
    )


def _lobby_private(
    settings: Settings,
    decks: Decks,
    sessions: SqliteSessionRecordStore,
    key_drafts: SqliteSourceKeyDrafts,
    lobby_auth: InstalledAuth,
) -> LobbyPrivate:
    """What only an admin of the lobby may reach: the host's socket, the people route.

    The people route exists only while a client is named to call it. Its
    accounts answer over the lock the sign-in holds, which deletes an account.
    """
    clients = settings.admitted_token_clients()
    return LobbyPrivate(
        voice=VoiceStatusUse(private=UdsSpeech(settings.speech_socket)),
        admin_people=AdminPeople(
            people=People(
                decks=decks,
                accounts=SqliteSubjectUserStore(settings.database, lobby_auth.lock),
                sessions=sessions,
                preferences=SqlitePersonPreferencesStore(settings.database),
                key_drafts=key_drafts,
            ),
            admitted_clients=clients,
            clock=SystemClock().now,
        )
        if clients
        else None,
    )


def installed_auths(
    settings: Settings,
    relying_party_for: RelyingPartyFor = relying_party,
) -> tuple[InstalledAuth, InstalledAuth]:
    """The lobby's sign-in wiring and the talk host's, over one lock and one store.

    Each host has its own relying party and its own signing key, so neither
    accepts the other's cookies or pending logins; both write the one
    `sessions` table, so they take the one write lock and the one account
    store, and report to the one audit sink.
    """
    lobby_client, talk_client = client_configs_of(settings)
    write_lock = ProcessWriteLock()
    lobby = InstalledAuth(
        config=web_auth_config_for(
            settings.secret_key,
            trusted_proxies=settings.trusted_proxies,
            origin=settings.public_origin,
        ),
        relying_party=relying_party_for(lobby_client),
        admission=admission_of(settings),
        accounts=SqliteSubjectUserStore(settings.database, write_lock),
        lock=write_lock,
        audit=LoggingAuditSink(),
        content_security_policy=content_security_policy(),
        talk_origin=settings.talk_origin,
    )
    talk = replace(
        lobby,
        config=web_auth_config_for(
            SecretStr(talk_session_secret(settings.secret_key.get_secret_value())),
            trusted_proxies=settings.trusted_proxies,
            origin=settings.talk_origin,
        ),
        relying_party=relying_party_for(talk_client),
        content_security_policy=talk_content_security_policy(),
    )
    return lobby, talk


def _pages(settings: Settings) -> Pages:
    return Pages(
        preferences=Preferences(
            instance=SqliteInstanceSettingsStore(settings.database),
            people=SqlitePersonPreferencesStore(settings.database),
            catalogs=load_catalogs(CATALOG_DIRECTORY),
        ),
        age_in_words=age_in_words,
        duration_in_words=duration_in_words,
    )


def _session_sweep(sessions: SqliteSessionRecordStore, clock: SystemClock) -> Schedule:
    """Delete expired sessions on a schedule, reading the clock on every run."""
    return Schedule(
        chore=lambda: sessions.delete_expired(clock.now()),
        interval=SESSION_SWEEP_INTERVAL,
    )


def _what_builds_a_deck(settings: Settings, *, bound: timedelta) -> DeckToolchain:
    """A container of the build's own, or this machine's own toolchain project.

    A deck is code (line 14a), so a build runs in a container unless this
    deployment asked in as many words for the development run. The daemon that
    would give it that container is asked what it is before anything is served,
    because a daemon too old to keep a build inside its own directory is not
    one this instance may run decks on at all.
    """
    if settings.build_runner is WhereBuildsRun.HOST:
        return HostToolchain(project=settings.toolchain, bound=bound)
    if settings.build_runner is WhereBuildsRun.WORKER:
        return _the_build_worker(settings, bound=bound)
    image, volume = settings.build_image, settings.build_volume
    if image is None or volume is None:
        raise cannot_start(_WITHOUT_A_SANDBOX)
    try:
        daemon = the_daemon_of_this_machine(image=image, disk=settings.build_disk)
    except DaemonRefusedError as refused:
        raise cannot_start(str(refused)) from None
    if settings.build_disk is None:
        _log.warning(_UNBOUNDED_BY_CHOICE)
    elif not daemon.bounds_a_container_filesystem:
        # Never silently: a machine that cannot bound what a build writes
        # beside its talk is one an operator agrees to in as many words.
        raise cannot_start(_NOTHING_BOUNDS_A_BUILD % daemon.storage_driver)
    return ContainerToolchain(
        image=image,
        volume=volume,
        memory=settings.build_memory,
        disk=settings.build_disk,
        bound=bound,
    )


def _the_build_worker(settings: Settings, *, bound: timedelta) -> WorkerToolchain:
    """The sealed build worker, which has to answer before anything is served."""
    toolchain = WorkerToolchain(
        socket_path=settings.build_worker_socket,
        megabytes=settings.build_output_megabytes,
        reply_bound_seconds=emergency_end(0.0, bound.total_seconds()),
        expected_uid=0,
        expected_gid=os.getegid(),
    )
    if not toolchain.is_answering():
        raise cannot_start(
            _WORKER_NOT_ANSWERING.format(window=toolchain.connect_window_seconds)
        )
    return toolchain


def web_auth_config_for(
    secret: SecretStr,
    *,
    trusted_proxies: str,
    origin: str,
) -> WebAuthConfig:
    """The library configuration one origin of this host runs: no Redis, a 12 hour cap.

    The cookies carry `Secure`, and the library's `__Host-` names: the origin
    is https, and a browser would not send a `Secure` cookie back over plain
    http. The only host this configuration answers to is the one people reach
    that origin under, and `secret` signs its cookies alone.
    """
    lifetime_seconds = int(SESSION_LIFETIME.total_seconds())
    return WebAuthConfig(
        session_secret=secret,
        trusted_proxies=TrustedProxies.parse(trusted_proxies),
        rate_limits=SingleProcessRateLimitBackend(),
        allowed_hosts_exact=frozenset({str(urlsplit(origin).hostname)}),
        allowed_hosts_patterns=(),
        session_lifetime=SessionLifetime(
            idle_seconds=lifetime_seconds,
            absolute_max_age_seconds=lifetime_seconds,
        ),
        cookie_samesite="lax",
        cookie_secure=True,
    )


def main() -> None:
    """Serve the lobby with the configuration the environment carries."""
    # The root logger drops INFO unless configured, and with it the sign-in
    # audit lines; uvicorn disables propagation only for its own loggers.
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    asyncio.run(_serve(build_instance(settings), settings))


def served_by_host(instance: Instance, settings: Settings) -> Router:
    """The app that answers each hostname, and nothing for any other.

    The `Host` header alone chooses: a request for a hostname this instance does
    not serve, with a port or without, gets the router's 404 and a socket is
    refused, so neither app ever answers under the other's name.
    """
    return Router(
        routes=[
            Host(str(urlsplit(settings.public_origin).hostname), instance.lobby),
            Host(str(urlsplit(settings.talk_origin).hostname), instance.talk),
        ],
    )


async def _serve(instance: Instance, settings: Settings) -> None:
    """Poll and sweep for as long as the server answers, and stop with it."""
    server = uvicorn.Server(
        uvicorn.Config(
            served_by_host(instance, settings),
            host=settings.host,
            port=settings.port,
            proxy_headers=False,
            # A path under /api/admin/ carries a subject, and a rejected WebSocket
            # handshake logs its path as an INFO record of uvicorn.error.
            access_log=False,
            log_level=logging.WARNING,
        ),
    )
    async with instance.poller.polling(), instance.session_sweep.polling():
        await server.serve()
