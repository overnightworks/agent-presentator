"""What varies by deployment. Credentials are the operator's Claude login."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ONE_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")
SOCKET_NAME = "copresenter.sock"


class Transport(StrEnum):
    """The private deployment transport or explicit developer transport."""

    UNIX = "unix"
    TCP = "tcp"


class Settings(BaseSettings):
    """Read from `COPRESENTER_*`."""

    model_config = SettingsConfigDict(
        env_prefix="COPRESENTER_",
        extra="ignore",
        populate_by_name=True,
    )

    transport: Transport = Transport.TCP
    allowed_origin: str | None = Field(
        default=None,
        validation_alias="COPRESENTER_ALLOWED_ORIGIN",
    )
    socket_directory: Path | None = None
    runtime_uid: int | None = Field(default=None, gt=0, validation_alias="PRESENTATOR_RUNTIME_UID")
    host: str = "127.0.0.1"
    port: int = 3040
    speech_url: str = "http://127.0.0.1:8090"
    deck: Path = Path("examples/copresenter-deck")
    claude_model: str = "claude-sonnet-4-6"
    language: str = "de"

    @field_validator("allowed_origin")
    @classmethod
    def _is_one_origin(cls, value: str | None) -> str | None:
        """A browser's `Origin` is scheme, host and optional port, and nothing else."""
        if value is None:
            return None
        if not _ONE_ORIGIN.fullmatch(value):
            message = (
                "COPRESENTER_ALLOWED_ORIGIN is one origin, http(s)://host[:port] "
                f"— no wildcard, credentials, path, query or fragment — not {value!r}"
            )
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _transport_has_its_owned_values(self) -> Settings:
        if self.transport is Transport.TCP and self.allowed_origin is None:
            message = "COPRESENTER_ALLOWED_ORIGIN is required for tcp transport"
            raise ValueError(message)
        if self.transport is Transport.UNIX:
            if self.socket_directory is None or not self.socket_directory.is_absolute():
                message = "COPRESENTER_SOCKET_DIRECTORY must be one absolute path"
                raise ValueError(message)
            if self.runtime_uid is None:
                message = "PRESENTATOR_RUNTIME_UID is required for unix transport"
                raise ValueError(message)
        return self

    @property
    def socket_path(self) -> Path:
        """The fixed socket name inside the operator-owned directory."""
        if self.socket_directory is None:
            message = "tcp transport has no Unix socket"
            raise RuntimeError(message)
        return self.socket_directory / SOCKET_NAME


def load_settings() -> Settings:
    """Read the non-secret configuration."""
    return Settings()
