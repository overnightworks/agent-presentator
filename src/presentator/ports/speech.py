"""The one read-only private speech capability Presentator uses."""

from typing import Protocol

from presentator.contracts.voice import VoiceStatus


class PrivateSpeech(Protocol):
    """Read one complete, verified speech catalogue snapshot."""

    async def voices(self) -> tuple[VoiceStatus, ...]:
        """Return the private service's complete status, or raise unavailable."""
        ...
