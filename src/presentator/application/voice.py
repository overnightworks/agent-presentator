"""Reconcile one private voice snapshot before the lobby can render it."""

from dataclasses import dataclass

from presentator.contracts.voice import VoiceId, VoiceStatus, VoiceUnavailableError
from presentator.ports.speech import PrivateSpeech

_CATALOGUE: tuple[VoiceId, ...] = (
    VoiceId.PIPER,
    VoiceId.CHATTERBOX,
    VoiceId.QWEN,
    VoiceId.VOXCPM,
    VoiceId.MAGPIE,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceStatusUse:
    """Use the private reader while preserving the public catalogue contract."""

    private: PrivateSpeech

    async def voices(self) -> tuple[VoiceStatus, ...]:
        """Return only a complete, ordered, non-duplicated catalogue."""
        snapshot = await self.private.voices()
        if tuple(row.id for row in snapshot) != _CATALOGUE:
            raise VoiceUnavailableError from None
        return snapshot
