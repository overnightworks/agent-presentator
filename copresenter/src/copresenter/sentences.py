"""Split a growing text stream into spoken sentences."""

from __future__ import annotations

import re

_TERMINATOR = re.compile(r"([.!?…][\"')\]]*)(\s+|$)")


def take_sentences(buffer: str) -> tuple[list[str], str]:
    """Pull complete sentences off the front of `buffer`.

    A sentence is complete when its terminator is followed by whitespace. A
    terminator at the very end stays in the remainder: the model may still be
    writing. Callers flush the remainder when the stream ends.
    """
    sentences: list[str] = []
    start = 0
    for match in _TERMINATOR.finditer(buffer):
        following = match.group(2)
        at_end = match.end() == len(buffer)
        if following == "" and at_end:
            break
        piece = buffer[start : match.end(1)].strip()
        if piece:
            sentences.append(piece)
        start = match.end()
    return sentences, buffer[start:]


def flush(buffer: str) -> str:
    """Return whatever is left once the model has stopped writing."""
    return buffer.strip()
