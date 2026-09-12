"""What varies by deployment; everything else is a constant beside its owner."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings
from webauth.proxies import TrustedProxies

# Every secret this instance is handed is at least this many characters, so no
# guard of it rests on a value somebody could type.
SECRET_LENGTH: Final = 32
# The one word an instance builds without a bound on what a container writes
# beside its talk for; anything blank is refused, because blank is not a word
# anybody said.
NO_BOUND_AT_ALL: Final = "none"

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


def _a_size_or_none(given: str | None) -> str | None:
    """Refuse a size that says nothing; `none` is how it is meant.

    A value that came out blank is an interpolation nobody watched, not a
    decision, and the decision it would stand for is to build without the one
    bound on what a deck writes beside its talk.
    """
    if given is None or given == NO_BOUND_AT_ALL:
        return None
    if not given.strip():
        message = (
            "is a size Docker takes, or the word"
            f" {NO_BOUND_AT_ALL} to build without that bound"
        )
        raise ValueError(message)
    return given


def _a_proxy_list(given: str) -> str:
    """Refuse a list that is not addresses or networks."""
    TrustedProxies.parse(given)
    return given


def _one_public_origin(given: str) -> str:
    """Require one canonical HTTP origin, without any address suffix."""
    parsed = urlsplit(given)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or given != f"{parsed.scheme}://{parsed.netloc}"
    ):
        message = "is one canonical http(s) origin without credentials or a path"
        raise ValueError(message)
    try:
        _port = parsed.port
    except ValueError as refused:
        message = "has an invalid port"
        raise ValueError(message) from refused
    return given


def _an_absolute_path(given: Path) -> Path:
    if not given.is_absolute():
        message = "is an absolute path"
        raise ValueError(message)
    return given


class Settings(BaseSettings, env_prefix="PRESENTATOR_", env_file=".env"):
    """Read from `PRESENTATOR_*` in the environment, or from a local `.env`."""

    secret_key: SecretStr = Field(min_length=SECRET_LENGTH)
    database: Path = Path("presentator.sqlite3")
    mirrors: Path = Path("mirrors")
    # The one directory a file-kind source's address may resolve under; the
    # image fixes it to the path a host directory is bound to, and a direct
    # run keeps it beside the checkout like every other path above.
    local_sources_mount: Path = Path("local-sources")
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
    # and in whole megabytes for the talk it may leave behind. A disk of `none`
    # is an instance that knowingly does without that bound, on a machine whose
    # storage driver will not hold a container's own filesystem to one.
    build_memory: str = "4g"
    build_disk: Annotated[str | None, AfterValidator(_a_size_or_none)] = "8g"
    build_output_megabytes: int = 300
    # Polling is what makes a push arrive at all, so it runs whether or not any
    # host ever calls the hook.
    source_poll_seconds: float = 300.0
    # A pull that hangs would hold the tick it runs on, so it is bounded.
    source_timeout_seconds: float = 20.0
    host: str = "127.0.0.1"
    port: int = 8000
    public_origin: Annotated[str, AfterValidator(_one_public_origin)]
    copresenter_socket: Annotated[Path, AfterValidator(_an_absolute_path)]
    speech_socket: Annotated[Path, AfterValidator(_an_absolute_path)] = Path(
        "/run/presentator-speech/speech.sock"
    )
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
