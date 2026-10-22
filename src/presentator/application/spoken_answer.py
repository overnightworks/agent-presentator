"""Turns an answer's token deltas into whole sentences for the voice."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from presentator.ports.copresenter import SentenceBoundaries


class SpokenAnswer:
    """Accumulates deltas and releases a sentence only once content follows it.

    The sentence at the buffer's end stays held back because the model may still
    be writing it: `Dr.` at the end is not yet a boundary.
    """

    def __init__(self, boundaries: SentenceBoundaries) -> None:
        """Hold the boundary function that finds sentences."""
        self._boundaries = boundaries
        self._buffer = ""

    def feed(self, delta: str) -> tuple[str, ...]:
        """Add a delta; return the sentences it completed."""
        text = self._buffer + delta
        *completed, _held = self._boundaries(text) or ("",)
        end = 0
        for sentence in completed:
            end = text.index(sentence, end) + len(sentence)
        self._buffer = text[end:].lstrip()
        return tuple(completed)

    def flush(self) -> tuple[str, ...]:
        """Return what is left once the model has stopped writing."""
        rest, self._buffer = self._buffer, ""
        return (rest,) if rest else ()
