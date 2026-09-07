"""What varies by deployment; everything else is a constant beside its owner."""

from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings

# Every secret this instance is handed is at least this many characters, so no
# guard of it rests on a value somebody could type.
SECRET_LENGTH: Final = 32

_REFUSED: Final = "this environment cannot start an instance"


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
        raise ConfigurationError(_what_is_wrong(refused)) from None


def _what_is_wrong(refused: ValidationError) -> str:
    named = ", ".join(
        f"{'.'.join(str(part) for part in fault['loc'])}: {fault['msg']}"
        for fault in refused.errors()
    )
    return f"{_REFUSED}: {named}"
