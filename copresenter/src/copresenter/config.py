"""What varies by deployment. Credentials are the operator's Claude login."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from `COPRESENTER_*`."""

    model_config = SettingsConfigDict(
        env_prefix="COPRESENTER_",
        extra="ignore",
        populate_by_name=True,
    )

    # Named by its full variable so a missing value fails with the name to set.
    allowed_origin: str = Field(validation_alias="COPRESENTER_ALLOWED_ORIGIN")
    host: str = "127.0.0.1"
    port: int = 3040
    speech_url: str = "http://127.0.0.1:8090"
    deck: Path = Path("examples/copresenter-deck")
    claude_model: str = "claude-sonnet-4-6"
    language: str = "de"


def load_settings() -> Settings:
    """Read the non-secret configuration."""
    return Settings()
