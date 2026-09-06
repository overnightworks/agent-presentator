"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

from datetime import timedelta
from functools import partial

import uvicorn
from fastapi import FastAPI

from presentator.adapters.catalog import (
    ENGLISH_CATALOG,
    age_in_words,
    load_lobby_text,
)
from presentator.adapters.decks import (
    ConfiguredSource,
    EnvironmentCredentials,
    MirroredDeckFolders,
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
from presentator.api.auth import create_lobby
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.host.config import Settings, load_settings


def build_lobby(settings: Settings) -> FastAPI:
    """Choose the adapter behind every port and hand the routes their use cases."""
    create_identity_tables(settings.database)
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
    decks = Decks(
        sources=ConfiguredSource(
            url=settings.source_url,
            ref=settings.source_ref,
            credential_reference=settings.source_credential,
            accounts=SqliteUserStore(settings.database),
        ),
        folders=MirroredDeckFolders(
            mirrors=settings.mirrors,
            credentials=EnvironmentCredentials(),
            pull_timeout=timedelta(seconds=settings.source_timeout_seconds),
        ),
        store=SqliteDeckStore(database=settings.database),
        clock=SystemClock(),
    )
    text = load_lobby_text(ENGLISH_CATALOG)
    return create_lobby(
        identity=identity,
        decks=decks,
        text=text,
        age_in_words=partial(age_in_words, language_tag=text.language_tag),
        secure_cookies=settings.https,
    )


def main() -> None:
    """Serve the lobby with the configuration the environment carries."""
    settings = load_settings()
    uvicorn.run(build_lobby(settings), host=settings.host, port=settings.port)
