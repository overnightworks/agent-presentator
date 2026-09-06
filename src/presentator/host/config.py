"""What varies by deployment; everything else is a constant beside its owner."""

from pathlib import Path
from typing import Final

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

SECRET_KEY_LENGTH: Final = 32


class Settings(BaseSettings, env_prefix="PRESENTATOR_", env_file=".env"):
    """Read from `PRESENTATOR_*` in the environment, or from a local `.env`."""

    secret_key: SecretStr = Field(min_length=SECRET_KEY_LENGTH)
    database: Path = Path("presentator.sqlite3")
    mirrors: Path = Path("mirrors")
    source_url: str | None = None
    source_ref: str = "main"
    # The name the source answers to in its hook address, and the secret a call
    # there has to carry; a stored source owns both once Settings does.
    source_name: str = "decks"
    source_hook_secret: SecretStr | None = None
    # Polling is what makes a push arrive at all, so it runs whether or not any
    # host ever calls the hook.
    source_poll_seconds: float = 300.0
    # The name of the environment variable holding the read-only secret, never
    # the secret itself, so no durable record of this instance carries a value.
    source_credential: str | None = None
    # A pull that hangs would hold the tick it runs on, so it is bounded.
    source_timeout_seconds: float = 20.0
    https: bool = False
    host: str = "127.0.0.1"
    port: int = 8000


def load_settings() -> Settings:
    """Read what the environment carries, refusing a missing or short key.

    pydantic-settings fills the fields while it validates, so this call is what
    reads `PRESENTATOR_*`; the constructor would ask for the required key as an
    argument instead of looking for it.
    """
    return Settings.model_validate({})
