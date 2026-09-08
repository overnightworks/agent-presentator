"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from webauth.config import WebAuthConfig
from webauth.liveness import IdleWindowLiveness
from webauth.proxies import TrustedProxies
from webauth.rate_limit import SingleProcessRateLimitBackend

from presentator.adapters.builds import SlidevBuilds
from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    duration_in_words,
    load_catalogs,
)
from presentator.adapters.decks import (
    MirroredConnectionChecker,
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
from presentator.adapters.secrets import connection_fingerprint_key, secret_box
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
from presentator.host.config import Settings, load_settings
from presentator.host.polling import SourcePoller


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
        identifiers=identifiers,
        box=box,
    )
    source_timeout = timedelta(seconds=settings.source_timeout_seconds)
    mirrors = SourceMirrors(
        directory=settings.mirrors,
        credentials=SourceCredentials(database=settings.database, box=box),
        pull_timeout=source_timeout,
    )
    build_bound = timedelta(seconds=settings.build_timeout_seconds)
    decks = Decks(
        sources=sources,
        folders=MirroredDeckFolders(mirrors=mirrors),
        store=SqliteDeckStore(database=settings.database),
        builder=SlidevBuilds(
            builds=settings.builds,
            toolchain=settings.toolchain,
            mirrors=mirrors,
            build_timeout=build_bound,
        ),
        source_runs=SqliteSourceRunStore(database=settings.database),
        # The same bound a scheduled pull takes: Check connection asks the
        # same remote for the same one thing, just without writing it down.
        checker=MirroredConnectionChecker(check_timeout=source_timeout),
        # One toolchain step's bound, which is what the refresh needs: it never
        # reads a live build, only what a process that is gone left behind.
        build_bound=build_bound,
        fingerprint_key=connection_fingerprint_key(
            settings.secret_key.get_secret_value(),
        ),
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
