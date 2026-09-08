"""Read slides and speaker notes from the deck folder itself.

Slidev owns the real parser. This reader is the honest subset a voice needs
tonight: Slidev's `---` separators and per-slide frontmatter, a heading as the
title, the last HTML comment as speaker notes. It is not a second slide map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SLIDES_FILE = "slides.md"
MANIFEST_FILE = "deck.toml"

_SEPARATOR = "---"
_FENCE_OPEN = re.compile(r"^\s*`+")
_NOTE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_TITLE_LINE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_MANIFEST_TITLE = re.compile(r"^title\s*=\s*\"([^\"]+)\"", re.MULTILINE)
_MANIFEST_LANGUAGE = re.compile(r"^language\s*=\s*\"([^\"]+)\"", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Slide:
    """One slide as the markdown carries it."""

    number: int
    title: str
    body: str
    notes: str


@dataclass(frozen=True, slots=True)
class Deck:
    """The talk this process was pointed at."""

    title: str
    language: str
    slides: tuple[Slide, ...]
    source: Path

    def at(self, number: int) -> Slide:
        """Return the slide with this 1-based number, or raise KeyError."""
        for slide in self.slides:
            if slide.number == number:
                return slide
        raise KeyError(number)


class DeckNotFoundError(FileNotFoundError):
    """The configured deck directory has no slides.md."""


def load_deck(directory: Path, *, language: str = "de") -> Deck:
    """Read `slides.md` and the optional `deck.toml` from this folder."""
    slides_path = directory / SLIDES_FILE
    if not slides_path.is_file():
        message = f"copresenter refuses to start: no {SLIDES_FILE} in {directory}"
        raise DeckNotFoundError(message)
    markdown = slides_path.read_text(encoding="utf-8")
    manifest_title, manifest_language = _manifest(directory / MANIFEST_FILE)
    slides = tuple(_slides(markdown))
    title = manifest_title or (slides[0].title if slides else directory.name)
    return Deck(
        title=title,
        language=manifest_language or language,
        slides=slides,
        source=directory,
    )


def _manifest(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "", ""
    text = path.read_text(encoding="utf-8")
    title_match = _MANIFEST_TITLE.search(text)
    language_match = _MANIFEST_LANGUAGE.search(text)
    title = title_match.group(1) if title_match else ""
    language = language_match.group(1) if language_match else ""
    return title, language


def _slides(markdown: str) -> list[Slide]:
    slides: list[Slide] = []
    number = 1
    for chunk in _slide_chunks(markdown):
        content, notes = _split_notes(_without_frontmatter(chunk))
        if not content.strip() and not notes:
            continue
        slides.append(
            Slide(
                number=number,
                title=_title(content),
                body=content.strip(),
                notes=notes,
            )
        )
        number += 1
    return slides


def _slide_chunks(markdown: str) -> list[str]:
    """Split on Slidev's slide separators, keeping per-slide frontmatter.

    A line of exactly `---` starts the next slide. When the following line is
    non-empty, that fence opens YAML frontmatter and the matching `---` closes
    it — it is not a slide of its own. Fences inside HTML comments or fenced
    code are not separators.
    """
    lines = markdown.splitlines()
    chunks: list[str] = []
    start = 0
    in_html_comment = False
    index = 0
    length = len(lines)
    while index < length:
        raw_line = lines[index]
        line = raw_line.rstrip()
        if in_html_comment:
            in_html_comment = _html_comment_continues(raw_line, inside=True)
            index += 1
            continue
        if line.startswith(_SEPARATOR):
            _collect_chunk(chunks, lines, start, index)
            next_line = lines[index + 1] if index + 1 < length else ""
            opens_frontmatter = _opens_frontmatter(line) and bool(next_line.strip())
            if opens_frontmatter:
                start = index
                index += 1
                while index < length and lines[index].rstrip() != _SEPARATOR:
                    index += 1
            else:
                start = index + 1
            index += 1
            continue
        fence = _FENCE_OPEN.match(line)
        if line.lstrip().startswith("```") and fence is not None:
            marker = fence.group(0)
            index += 1
            while index < length and not lines[index].startswith(marker):
                index += 1
            if index < length:
                index += 1
            continue
        in_html_comment = _html_comment_continues(raw_line, inside=False)
        index += 1
    if start < length:
        _collect_chunk(chunks, lines, start, length)
    return chunks


def _opens_frontmatter(line: str) -> bool:
    return not line.startswith("----")


def _collect_chunk(chunks: list[str], lines: list[str], start: int, end: int) -> None:
    if start >= end:
        return
    chunks.append("\n".join(lines[start:end]))


def _without_frontmatter(chunk: str) -> str:
    lines = chunk.splitlines()
    if not lines or lines[0].strip() != _SEPARATOR:
        return chunk
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _SEPARATOR:
            return "\n".join(lines[index + 1 :])
    return chunk


def _html_comment_continues(line: str, *, inside: bool) -> bool:
    cursor = 0
    while cursor < len(line):
        if inside:
            end = line.find("-->", cursor)
            if end < 0:
                return True
            inside = False
            cursor = end + 3
            continue
        start = line.find("<!--", cursor)
        if start < 0:
            return False
        end = line.find("-->", start + 4)
        if end < 0:
            return True
        cursor = end + 3
    return inside


def _split_notes(chunk: str) -> tuple[str, str]:
    matches = list(_NOTE.finditer(chunk))
    if not matches:
        return chunk, ""
    last = matches[-1]
    notes = last.group(1).strip()
    content = f"{chunk[: last.start()]}{chunk[last.end() :]}"
    return content, notes


def _title(body: str) -> str:
    match = _TITLE_LINE.search(body)
    if match:
        return match.group(1).strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    return "Untitled"
