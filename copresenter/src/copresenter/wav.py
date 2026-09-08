"""A 16-bit PCM mono WAV, as the speech contract answers."""

from __future__ import annotations

import math
import struct

SAMPLE_RATE = 16000
_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


def wrap_pcm(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Prefix raw 16-bit little-endian mono PCM with a WAV header."""
    return (
        _HEADER.pack(
            b"RIFF",
            36 + len(pcm),
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            sample_rate,
            sample_rate * 2,
            2,
            16,
            b"data",
            len(pcm),
        )
        + pcm
    )


def tone(
    *,
    seconds: float = 0.4,
    frequency: float = 440.0,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.2,
) -> bytes:
    """A short sine wave, used by the stand-in when nothing local speaks yet."""
    frames = int(seconds * sample_rate)
    pcm = bytearray()
    for index in range(frames):
        sample = amplitude * math.sin(2 * math.pi * frequency * index / sample_rate)
        pcm.extend(struct.pack("<h", int(sample * 32767)))
    return wrap_pcm(bytes(pcm), sample_rate=sample_rate)
