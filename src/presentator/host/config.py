"""What varies by deployment; everything else is a constant beside its owner."""

from pathlib import Path
from typing import Final

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

SECRET_KEY_LENGTH: Final = 32


class Settings(
    BaseSettings,
    env_prefix="PRESENTATOR_",
    env_file=".env",
    validate_default=True,
):
    """Read from `PRESENTATOR_*` in the environment, or from a local `.env`."""

    # An instance has no key of its own: the empty default is what the length
    # rule refuses, so a missing PRESENTATOR_SECRET_KEY stops the start loudly.
    secret_key: SecretStr = Field(default=SecretStr(""), min_length=SECRET_KEY_LENGTH)
    database: Path = Path("presentator.sqlite3")
    https: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
