"""What varies by deployment. Credentials are the operator's Claude login."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ONE_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")


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

    @field_validator("allowed_origin")
    @classmethod
    def _is_one_origin(cls, value: str) -> str:
        """A browser's `Origin` is scheme, host and optional port, and nothing else."""
        if not _ONE_ORIGIN.fullmatch(value):
            message = (
                "COPRESENTER_ALLOWED_ORIGIN is one origin, http(s)://host[:port] "
                f"— no wildcard, credentials, path, query or fragment — not {value!r}"
            )
            raise ValueError(message)
        return value


def load_settings() -> Settings:
    """Read the non-secret configuration."""
    return Settings()
