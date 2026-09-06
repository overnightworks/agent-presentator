"""Composition root: builds the adapters, wires the application, serves the API.

Imports every other layer, because it is the one place that decides which
adapter satisfies which port.
"""

import uvicorn
from fastapi import FastAPI

from presentator.adapters.catalog import CATALOG_DIRECTORY, load_catalogs
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
from presentator.application.identity import Identity
from presentator.application.preferences import Preferences
from presentator.host.config import Settings, load_settings


def build_lobby(settings: Settings) -> FastAPI:
    """Choose the adapter behind every port and hand the routes their use cases."""
    create_identity_tables(settings.database)
    create_preference_tables(settings.database)
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
    preferences = Preferences(
        instance=SqliteInstanceSettingsStore(settings.database),
        people=SqlitePersonPreferencesStore(settings.database),
        catalogs=load_catalogs(CATALOG_DIRECTORY),
    )
    return create_lobby(
        identity=identity,
        preferences=preferences,
        secure_cookies=settings.https,
    )


def main() -> None:
    """Serve the lobby with the configuration the environment carries."""
    settings = load_settings()
    uvicorn.run(build_lobby(settings), host=settings.host, port=settings.port)
