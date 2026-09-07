"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import uvicorn
from fastapi import APIRouter, FastAPI

from presentator.adapters.builds import SlidevBuilds
from presentator.adapters.catalog import (
    CATALOG_DIRECTORY,
    age_in_words,
    load_catalogs,
)
from presentator.adapters.decks import (
    ConfiguredSource,
    EnvironmentCredentials,
    MirroredDeckFolders,
    SourceMirrors,
    SqliteDeckStore,
    create_deck_tables,
)
from presentator.adapters.identity import (
    Argon2PasswordHasher,
    HmacSessionCookieSigner,
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
from presentator.api.auth import create_lobby
from presentator.api.hooks import fetch_hook
from presentator.api.pages import Pages
from presentator.application.decks import Decks
from presentator.application.identity import Identity
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
    identity = Identity(
        users=SqliteUserStore(settings.database),
        sessions=SqliteSessionRecordStore(settings.database),
        attempts=SqliteLoginAttemptStore(settings.database),
        hasher=Argon2PasswordHasher(),
        clock=SystemClock(),
        identifiers=TokenIdentifierFactory(),
        cookies=HmacSessionCookieSigner(
            settings.secret_key.get_secret_value().encode(),
        ),
    )
    mirrors = SourceMirrors(
        directory=settings.mirrors,
        credentials=EnvironmentCredentials(),
        pull_timeout=timedelta(seconds=settings.source_timeout_seconds),
    )
    decks = Decks(
        sources=ConfiguredSource(
            url=settings.source_url,
            ref=settings.source_ref,
            credential_reference=settings.source_credential,
            accounts=SqliteUserStore(settings.database),
        ),
        folders=MirroredDeckFolders(mirrors=mirrors),
        store=SqliteDeckStore(database=settings.database),
        builder=SlidevBuilds(
            builds=settings.builds,
            toolchain=settings.toolchain,
            mirrors=mirrors,
            build_timeout=timedelta(seconds=settings.build_timeout_seconds),
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
    )
    # One use case object serves both callers, so the hook and the poll share
    # the one refresh that runs at a time.
    return Instance(
        lobby=create_lobby(
            identity=identity,
            decks=decks,
            pages=pages,
            secure_cookies=settings.https,
            fetch_hook=_armed_hook(settings, decks),
        ),
        poller=SourcePoller(
            refresh=decks.refresh,
            interval=timedelta(seconds=settings.source_poll_seconds),
        ),
    )


def _armed_hook(settings: Settings, decks: Decks) -> APIRouter | None:
    """The hook's route once a secret arms it; without one there is no address."""
    secret = settings.source_hook_secret
    if secret is None:
        return None
    return fetch_hook(
        decks=decks,
        source=settings.source_name,
        secret=secret.get_secret_value(),
    )


def main() -> None:
    """Serve the lobby with the configuration the environment carries."""
    settings = load_settings()
    asyncio.run(_serve(build_instance(settings), settings))


async def _serve(instance: Instance, settings: Settings) -> None:
    """Poll for as long as the server answers, and stop with it."""
    server = uvicorn.Server(
        uvicorn.Config(instance.lobby, host=settings.host, port=settings.port),
    )
    async with instance.poller.polling():
        await server.serve()
