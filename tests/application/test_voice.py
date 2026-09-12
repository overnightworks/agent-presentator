"""The application exposes only a complete canonical voice catalogue."""

import asyncio
from dataclasses import dataclass

import pytest

from presentator.application.voice import VoiceStatusUse
from presentator.contracts.voice import (
    VoiceId,
    VoiceLoadOutcome,
    VoiceSnapshot,
    VoiceState,
    VoiceStatus,
    VoiceUnavailableError,
)


@dataclass
class FakeSpeech:
    """A private reader with the exact snapshot the test needs."""

    voices: tuple[VoiceStatus, ...]
    load_outcome: VoiceLoadOutcome = VoiceLoadOutcome.ACTIVATED

    async def snapshot(self) -> VoiceSnapshot:
        return VoiceSnapshot(voices=self.voices, recovery=None)

    async def load(self, voice: VoiceId) -> VoiceLoadOutcome:
        del voice
        return self.load_outcome


@pytest.mark.parametrize(
    "snapshot",
    [
        (
            VoiceStatus(
                id=VoiceId.PIPER,
                name="Piper",
                language="German — Thorsten voice",
                state=VoiceState.ACTIVE,
            ),
        ),
        tuple(
            VoiceStatus(
                id=VoiceId.PIPER,
                name="Piper",
                language="German — Thorsten voice",
                state=VoiceState.ACTIVE,
            )
            for _ in VoiceId
        ),
        tuple(
            VoiceStatus(
                id=voice_id,
                name=voice_id.value,
                language="German — Thorsten voice",
                state=VoiceState.ACTIVE,
            )
            for voice_id in reversed(tuple(VoiceId))
        ),
    ],
)
def test_voice_status_rejects_an_incomplete_duplicated_or_reordered_private_snapshot(
    snapshot: tuple[VoiceStatus, ...],
) -> None:
    reader = VoiceStatusUse(private=FakeSpeech(snapshot))

    async def read() -> None:
        with pytest.raises(VoiceUnavailableError):
            await reader.snapshot()

    asyncio.run(read())


def test_voice_load_preserves_the_durability_uncertain_outcome() -> None:
    reader = VoiceStatusUse(
        private=FakeSpeech(
            (),
            load_outcome=VoiceLoadOutcome.ACTIVATED_DURABILITY_UNCONFIRMED,
        )
    )

    assert (
        asyncio.run(reader.load(VoiceId.PIPER))
        is VoiceLoadOutcome.ACTIVATED_DURABILITY_UNCONFIRMED
    )
