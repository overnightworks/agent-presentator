"""The verified voice rows the authenticated lobby may show."""

from dataclasses import dataclass
from enum import StrEnum


class VoiceId(StrEnum):
    """The closed, ordered administration catalogue."""

    PIPER = "piper"
    CHATTERBOX = "chatterbox"
    QWEN = "qwen3-tts-0.6b"
    VOXCPM = "voxcpm2"
    MAGPIE = "nvidia-magpie"


class VoiceState(StrEnum):
    """A state whose source is the private speech process."""

    ACTIVE = "active"
    LOADING = "loading"
    DOWNLOADED = "downloaded"
    NOT_DOWNLOADED = "not_downloaded"
    UNAVAILABLE = "unavailable"


class VoiceUnavailableError(RuntimeError):
    """The complete status snapshot could not be verified."""


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceStatus:
    """One status row passed from the private speech process."""

    id: VoiceId
    name: str
    language: str
    state: VoiceState
