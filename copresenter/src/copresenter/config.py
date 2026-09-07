"""What varies by deployment. Credentials are the operator's Claude login."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from `COPRESENTER_*`."""

    model_config = SettingsConfigDict(env_prefix="COPRESENTER_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 3040
    speech_url: str = "http://127.0.0.1:8090"
    deck: Path = Path("examples/copresenter-deck")
    claude_model: str = "claude-sonnet-4-6"
    language: str = "de"


def load_settings() -> Settings:
    """Read the non-secret configuration."""
    return Settings()
