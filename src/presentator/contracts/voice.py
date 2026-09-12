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


class SampleLanguage(StrEnum):
    """The closed browser-to-speech language token for a fixed voice sample."""

    GERMAN = "de"
    ENGLISH = "en"


class VoiceState(StrEnum):
    """A state whose source is the private speech process."""

    ACTIVE = "active"
    LOADING = "loading"
    FAILED = "failed"
    DOWNLOADED = "downloaded"
    NOT_DOWNLOADED = "not_downloaded"
    UNAVAILABLE = "unavailable"


class VoiceUnavailableError(RuntimeError):
    """The complete status snapshot could not be verified."""


class VoiceSampleBusyError(VoiceUnavailableError):
    """A current public or private synthesis owns the one active voice."""


class VoiceLoadOutcome(StrEnum):
    """The private Load result an authenticated caller may act on."""

    ACTIVATED = "activated"
    ACTIVATED_DURABILITY_UNCONFIRMED = "activated_durability_unconfirmed"
    NOT_ACTIVATED = "not_activated"


class VoiceRecoveryKind(StrEnum):
    """The private explanation for a recoverable unavailable voice."""

    INVALID_SELECTION = "invalid_selection"
    LOAD_FAILED = "load_failed"
    DURABILITY_UNCONFIRMED = "durability_unconfirmed"


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceStatus:
    """One status row passed from the private speech process."""

    id: VoiceId
    name: str
    language: str
    state: VoiceState


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceRecovery:
    """A non-sensitive recovery fact attached to the complete snapshot."""

    kind: VoiceRecoveryKind
    voice: VoiceId | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceSnapshot:
    """The complete closed catalogue and one recoverable selection fact."""

    voices: tuple[VoiceStatus, ...]
    recovery: VoiceRecovery | None
