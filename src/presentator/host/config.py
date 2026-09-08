"""What varies by deployment; everything else is a constant beside its owner."""

from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings
from webauth.proxies import TrustedProxies

# Every secret this instance is handed is at least this many characters, so no
# guard of it rests on a value somebody could type.
SECRET_LENGTH: Final = 32

_REFUSED: Final = "this environment cannot start an instance"


class ConfigurationError(ValueError):
    """What the environment carries is not a configuration to run on."""


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
    # Polling is what makes a push arrive at all, so it runs whether or not any
    # host ever calls the hook.
    source_poll_seconds: float = 300.0
    # A pull that hangs would hold the tick it runs on, so it is bounded.
    source_timeout_seconds: float = 20.0
    https: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    # Empty: the login budget keys on the ASGI peer. A list is the peers whose
    # X-Forwarded-For this instance believes.
    trusted_proxies: Annotated[str, AfterValidator(_a_proxy_list)] = ""


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
