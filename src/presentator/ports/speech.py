"""The private speech status and Load capability Presentator uses."""

from typing import Protocol

from presentator.contracts.voice import (
    SampleLanguage,
    VoiceId,
    VoiceLoadOutcome,
    VoiceSnapshot,
)


class PrivateSpeech(Protocol):
    """Read one verified speech snapshot and synchronously Load a downloaded voice."""

    async def snapshot(self) -> VoiceSnapshot:
        """Return the private service's complete status, or raise unavailable."""
        ...

    async def load(self, voice: VoiceId) -> VoiceLoadOutcome:
        """Ask the private service to synchronously load a downloaded voice."""
        ...

    async def sample(self, voice: VoiceId, language: SampleLanguage) -> bytes:
        """Return one fixed-phrase WAV through the verified active voice."""
        ...
