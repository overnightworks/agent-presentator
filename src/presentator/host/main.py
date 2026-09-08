"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from webauth.config import WebAuthConfig
from webauth.liveness import IdleWindowLiveness
from webauth.proxies import TrustedProxies
from webauth.rate_limit import SingleProcessRateLimitBackend

from presentator.adapters.builds import (
    ContainerToolchain,
    DaemonRefusedError,
    DeckToolchain,
    HostToolchain,
    SlidevBuilds,
    the_daemon_of_this_machine,
)
from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    duration_in_words,
    load_catalogs,
)
from presentator.adapters.decks import (
    ConfiguredSource,
    EnvironmentCredentials,
    MirroredDeckFolders,
    SourceCredentials,
    SourceMirrors,
    SqliteDeckStore,
    SqliteSourceRunStore,
    SqliteSourceStore,
    create_deck_tables,
)
from presentator.adapters.identity import (
    Argon2PasswordHasher,
    IdleWindow,
    SignedSessionCookie,
    SqliteLoginAttemptStore,
    SqliteSessionRecordStore,
    SqliteUserStore,
    SystemClock,
    TokenIdentifierFactory,
    create_identity_tables,
)
from presentator.adapters.preferences import (
    SqliteInstanceSettingsStore,
    SqlitePersonPreferencesStore,
    create_preference_tables,
)
from presentator.adapters.secrets import secret_box
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
from presentator.host.config import (
    Settings,
    WhereBuildsRun,
    cannot_start,
    load_settings,
)
from presentator.host.polling import SourcePoller

_WITHOUT_A_SANDBOX = (
    "build_image and build_volume say what a deck's build runs in and where it"
    " writes, and a deck is code (line 14a): without both of them there is no"
    " sandbox. Ask for the development run knowingly with"
    " build_runner=host, or name them"
)
_NOTHING_BOUNDS_A_BUILD = (
    "the %s storage driver cannot hold a container's own filesystem to a size,"
    " so what one build writes outside its talk is bounded by this machine alone"
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Instance:
    """What a running instance is: the addresses, and the polling beside them."""

    lobby: FastAPI
    poller: SourcePoller


def build_instance(settings: Settings) -> Instance:
    """Choose the adapter behind every port and hand the routes their use cases."""
    create_identity_tables(settings.database)
    create_preference_tables(settings.database)
    create_deck_tables(settings.database)
    accounts = SqliteUserStore(settings.database)
    identifiers = TokenIdentifierFactory()
    clock = SystemClock()
    hasher = Argon2PasswordHasher()
    liveness = IdleWindowLiveness(int(IDLE_WINDOW.total_seconds()))
    web_auth = _web_auth_config(
        settings.secret_key,
        hasher=hasher,
        liveness=liveness,
        trusted_proxies=settings.trusted_proxies,
    )
    # One box for both directions: what the store writes down is what the
    # resolver opens at the pull, and neither picks its own key.
    box = secret_box(settings.secret_key.get_secret_value())
    identity = Identity(
        users=accounts,
        sessions=SqliteSessionRecordStore(settings.database, clock=clock),
        attempts=SqliteLoginAttemptStore(settings.database, clock=clock),
        hasher=hasher,
        clock=clock,
        identifiers=identifiers,
        cookies=SignedSessionCookie(settings.secret_key.get_secret_value().encode()),
        liveness=IdleWindow(liveness),
    )
    sources = SqliteSourceStore(
        database=settings.database,
        configured=ConfiguredSource(
            name=settings.source_name,
            url=settings.source_url,
            ref=settings.source_ref,
            credential_reference=settings.source_credential,
            accounts=accounts,
        ),
        identifiers=identifiers,
        box=box,
    )
    # A file written before sources were rows carries decks that name none, so
    # the row they belong to is written before the first page reads them; on an
    # empty instance there is no admin to own it yet, and the refresh that runs
    # after first start writes it then.
    sources.seed()
    mirrors = SourceMirrors(
        directory=settings.mirrors,
        credentials=SourceCredentials(
            database=settings.database,
            box=box,
            environment=EnvironmentCredentials(),
        ),
        pull_timeout=timedelta(seconds=settings.source_timeout_seconds),
    )
    build_bound = timedelta(seconds=settings.build_timeout_seconds)
    decks = Decks(
        sources=sources,
        folders=MirroredDeckFolders(mirrors=mirrors),
        store=SqliteDeckStore(database=settings.database),
        builder=SlidevBuilds(
            builds=settings.builds,
            toolchain=_what_builds_a_deck(settings, bound=build_bound),
            mirrors=mirrors,
            output_megabytes=settings.build_output_megabytes,
        ),
        source_runs=SqliteSourceRunStore(database=settings.database),
        # One toolchain step's bound, which is what the refresh needs: it never
        # reads a live build, only what a process that is gone left behind.
        build_bound=build_bound,
        clock=SystemClock(),
    )
    pages = Pages(
        preferences=Preferences(
            instance=SqliteInstanceSettingsStore(settings.database),
            people=SqlitePersonPreferencesStore(settings.database),
            catalogs=load_catalogs(CATALOG_DIRECTORY),
        ),
        age_in_words=age_in_words,
        duration_in_words=duration_in_words,
    )
    # One use case object serves both callers, so the hook and the poll share
    # the one refresh that runs at a time.
    return Instance(
        lobby=create_lobby(
            identity=identity,
            decks=decks,
            pages=pages,
            auth=InstalledAuth(
                config=web_auth,
                secure_cookies=settings.https,
            ),
        ),
        poller=SourcePoller(
            refresh=decks.refresh,
            interval=timedelta(seconds=settings.source_poll_seconds),
        ),
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
    image, volume = settings.build_image, settings.build_volume
    if image is None or volume is None:
        raise cannot_start(_WITHOUT_A_SANDBOX)
    try:
        daemon = the_daemon_of_this_machine()
    except DaemonRefusedError as refused:
        raise cannot_start(str(refused)) from None
    if not daemon.bounds_a_container_filesystem:
        _log.warning(_NOTHING_BOUNDS_A_BUILD, daemon.storage_driver)
    return ContainerToolchain(
        image=image,
        volume=volume,
        memory=settings.build_memory,
        # A driver that cannot hold a container's filesystem to a size refuses
        # the option rather than bounding it, and would fail every build.
        disk=settings.build_disk if daemon.bounds_a_container_filesystem else None,
        bound=bound,
    )


def _web_auth_config(
    secret: SecretStr,
    *,
    hasher: Argon2PasswordHasher,
    liveness: IdleWindowLiveness,
    trusted_proxies: str,
) -> WebAuthConfig:
    """The library configuration this host runs: Argon2id, no Redis, last_seen."""
    idle_seconds = int(IDLE_WINDOW.total_seconds())
    failure_seconds = int(FAILURE_WINDOW.total_seconds())
    return WebAuthConfig(
        session_secret=secret,
        trusted_proxies=TrustedProxies.parse(trusted_proxies),
        password_hasher=hasher,
        rate_limits=SingleProcessRateLimitBackend(),
        allowed_hosts_exact=frozenset(),
        allowed_hosts_patterns=(),
        session_max_age_seconds=idle_seconds,
        session_liveness=liveness,
        login_rate_limit=FAILURES_BEFORE_THROTTLE,
        login_lockout_threshold=FAILURES_BEFORE_THROTTLE,
        login_lockout_window_seconds=failure_seconds,
        login_rate_window_seconds=failure_seconds,
        session_cookie_name=SESSION_COOKIE,
        session_cache=None,
    )


def main() -> None:
    """Serve the lobby with the configuration the environment carries."""
    settings = load_settings()
    asyncio.run(_serve(build_instance(settings), settings))


async def _serve(instance: Instance, settings: Settings) -> None:
    """Poll for as long as the server answers, and stop with it."""
    server = uvicorn.Server(
        uvicorn.Config(
            instance.lobby,
            host=settings.host,
            port=settings.port,
            proxy_headers=False,
        ),
    )
    async with instance.poller.polling():
        await server.serve()
