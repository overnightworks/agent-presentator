"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

import uvicorn
from fastapi import FastAPI

from presentator.adapters.catalog import ENGLISH_CATALOG, load_lobby_text
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
from presentator.application.identity import Identity
from presentator.host.config import Settings, load_settings


def build_lobby(settings: Settings) -> FastAPI:
    """Choose the adapter behind every port and hand the routes their use cases."""
    create_identity_tables(settings.database)
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
    return create_lobby(
        identity=identity,
        text=load_lobby_text(ENGLISH_CATALOG),
        secure_cookies=settings.https,
    )


def main() -> None:
    """Serve the lobby with the configuration the environment carries."""
    settings = load_settings()
    uvicorn.run(build_lobby(settings), host=settings.host, port=settings.port)
