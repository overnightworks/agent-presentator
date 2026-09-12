"""Reconcile one private voice snapshot before the lobby can render it."""

from dataclasses import dataclass

from presentator.contracts.voice import (
    VoiceId,
    VoiceLoadOutcome,
    VoiceSnapshot,
    VoiceUnavailableError,
)
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

    async def snapshot(self) -> VoiceSnapshot:
        """Return one complete verified status snapshot."""
        snapshot = await self.private.snapshot()
        if tuple(row.id for row in snapshot.voices) != _CATALOGUE:
            raise VoiceUnavailableError from None
        return snapshot

    async def load(self, voice: VoiceId) -> VoiceLoadOutcome:
        """Pass the closed target through the private adapter."""
        return await self.private.load(voice)
