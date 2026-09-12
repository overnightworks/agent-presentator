"""The application exposes only a complete canonical voice catalogue."""

import asyncio
from dataclasses import dataclass

import pytest

from presentator.application.voice import VoiceStatusUse
from presentator.contracts.voice import (
    VoiceId,
    VoiceState,
    VoiceStatus,
    VoiceUnavailableError,
)


@dataclass
class FakeSpeech:
    """A private reader with the exact snapshot the test needs."""

    snapshot: tuple[VoiceStatus, ...]

    async def voices(self) -> tuple[VoiceStatus, ...]:
        return self.snapshot


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
            await reader.voices()

    asyncio.run(read())
