"""Sentence boundaries of German speech text, found by syntok."""

from __future__ import annotations

import re

# syntok ships no type stubs, so pyright cannot know its types.
from syntok import segmenter  # pyright: ignore[reportMissingTypeStubs]

_ENDS_IN_ORDINAL = re.compile(r"\d+\.$")


def split_sentences(text: str) -> tuple[str, ...]:
    """The sentences of `text`; the last one may still be unfinished.

    A number and its period read as a German ordinal (`Am 3. Mai`), so the
    sentence goes on after it instead of ending there.
    """
    merged: list[str] = []
    for sentence in _segmented(text):
        if merged and _ENDS_IN_ORDINAL.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    return tuple(merged)


def _segmented(text: str) -> tuple[str, ...]:
    return tuple(
        "".join(token.spacing + token.value for token in sentence).strip()
        for paragraph in segmenter.analyze(text)  # pyright: ignore[reportUnknownMemberType]
        for sentence in paragraph
    )
