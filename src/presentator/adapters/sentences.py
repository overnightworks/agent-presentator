"""Sentence boundaries of German speech text, found by syntok."""

from __future__ import annotations

# syntok ships no type stubs, so pyright cannot know its types.
from syntok import segmenter  # pyright: ignore[reportMissingTypeStubs]


def split_sentences(text: str) -> tuple[str, ...]:
    """The sentences of `text`; the last one may still be unfinished."""
    return tuple(
        "".join(token.spacing + token.value for token in sentence).strip()
        for paragraph in segmenter.analyze(text)  # pyright: ignore[reportUnknownMemberType]
        for sentence in paragraph
    )
