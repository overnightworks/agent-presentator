"""What varies by deployment; the provider key stays out of this object."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_MISSING_KEY = "copresenter refuses to start: ANTHROPIC_API_KEY is not set"
_PROVIDER_KEY_ENV = "ANTHROPIC_API_KEY"


class Settings(BaseSettings):
    """Read from `COPRESENTER_*`. The Anthropic key is not a field here."""

    model_config = SettingsConfigDict(env_prefix="COPRESENTER_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 3040
    speech_url: str = "http://127.0.0.1:8765"
    deck: Path = Path("examples/copresenter-deck")
    claude_model: str = "claude-sonnet-4-6"
    language: str = "de"


class MissingProviderKeyError(SystemExit):
    """The process was asked to start without a provider credential."""


def provider_key() -> str:
    """Return the Anthropic key from the environment, or refuse to start.

    The value is never logged and never written. It is not a Settings field, so
    a dumped configuration cannot carry it.
    """
    key = os.environ.get(_PROVIDER_KEY_ENV, "").strip()
    if not key:
        raise MissingProviderKeyError(_MISSING_KEY)
    return key


def load_settings() -> Settings:
    """Read the non-secret configuration."""
    return Settings()
