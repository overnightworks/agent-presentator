"""What varies by deployment; everything else is a constant beside its owner."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings
from webauth.proxies import TrustedProxies

# Every secret this instance is handed is at least this many characters, so no
# guard of it rests on a value somebody could type.
SECRET_LENGTH: Final = 32

_REFUSED: Final = "this environment cannot start an instance"


class WhereBuildsRun(StrEnum):
    """Where the toolchain that builds a deck runs (line 14a).

    A container of the build's own is what an instance is; this machine itself
    is a development run, and a deployment gets it only by saying so.
    """

    CONTAINER = "container"
    HOST = "host"


class ConfigurationError(ValueError):
    """What the environment carries is not a configuration to run on."""


def _a_real_secret(given: SecretStr) -> SecretStr:
    """Refuse a secret too short or too blank to guard anything.

    An empty value would otherwise arm the hook with a secret every caller
    already carries, which is worse than the closed hook it looks like.
    """
    if len(given.get_secret_value().strip()) < SECRET_LENGTH:
        message = (
            f"is at least {SECRET_LENGTH} characters that are not blank;"
            " leave it unset to keep the hook closed"
        )
        raise ValueError(message)
    return given


def _a_proxy_list(given: str) -> str:
    """Refuse a list that is not addresses or networks."""
    TrustedProxies.parse(given)
    return given


class Settings(BaseSettings, env_prefix="PRESENTATOR_", env_file=".env"):
    """Read from `PRESENTATOR_*` in the environment, or from a local `.env`."""

    secret_key: SecretStr = Field(min_length=SECRET_LENGTH)
    database: Path = Path("presentator.sqlite3")
    mirrors: Path = Path("mirrors")
    # Where built talks are kept, and the Node project whose Slidev builds
    # them; both are places on the machine the instance runs on.
    builds: Path = Path("builds")
    toolchain: Path = Path("frontend")
    # A build that hangs would hold every later build behind it, so each step
    # of the toolchain is bounded.
    build_timeout_seconds: float = 300.0
    # A deck is code, so every build runs in a container of its own: the image
    # the toolchain stands in, and the volume the builds root is a directory
    # of, are what a deployment names for it. Running the toolchain on this
    # machine instead is a development run, and it takes saying so.
    build_runner: WhereBuildsRun = WhereBuildsRun.CONTAINER
    build_image: str | None = None
    build_volume: str | None = None
    # What one build may take of this machine, in Docker's own words for a size
    # and in whole megabytes for the talk it may leave behind.
    build_memory: str = "4g"
    build_disk: str | None = "8g"
    build_output_megabytes: int = 300
    source_url: str | None = None
    source_ref: str = "main"
    # The name the source answers to in its hook address, and the secret a call
    # there has to carry; a stored source owns both once Settings does. Without
    # a secret there is no hook address at all, only the poll.
    source_name: str = "decks"
    source_hook_secret: Annotated[SecretStr, AfterValidator(_a_real_secret)] | None = (
        None
    )
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
    # Empty: the login budget keys on the ASGI peer. A list is the peers whose
    # X-Forwarded-For this instance believes.
    trusted_proxies: Annotated[str, AfterValidator(_a_proxy_list)] = ""


def cannot_start(reason: str) -> ConfigurationError:
    """The refusal an environment no instance can run on is answered with."""
    return ConfigurationError(f"{_REFUSED}: {reason}")


def load_settings() -> Settings:
    """Read what the environment carries, refusing what cannot start an instance.

    pydantic-settings fills the fields while it validates, so this call is what
    reads `PRESENTATOR_*`; the constructor would ask for the required key as an
    argument instead of looking for it.
    """
    try:
        return Settings.model_validate({})
    except ValidationError as refused:
        # A refused value is often a real secret, and the library's own message
        # quotes it; only the field and the reason may leave this call.
        raise cannot_start(_what_is_wrong(refused)) from None


def _what_is_wrong(refused: ValidationError) -> str:
    return ", ".join(
        f"{'.'.join(str(part) for part in fault['loc'])}: {fault['msg']}"
        for fault in refused.errors()
    )
