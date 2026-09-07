"""Read slides and speaker notes from the deck folder itself.

Slidev owns the real parser. This reader is the honest subset a voice needs
tonight: `---` separated slides, a heading as the title, the last HTML comment
as speaker notes. It is not a second slide map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SLIDES_FILE = "slides.md"
MANIFEST_FILE = "deck.toml"

_FRONTMATTER_OPEN = "---"
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
    body = markdown
    if body.startswith(_FRONTMATTER_OPEN):
        rest = body[len(_FRONTMATTER_OPEN) :]
        _frontmatter, separator, remainder = rest.partition(f"\n{_FRONTMATTER_OPEN}")
        if not separator:
            return []
        body = remainder.lstrip("\n")
    chunks = re.split(r"\n---\n", body)
    slides: list[Slide] = []
    for index, chunk in enumerate(chunks, start=1):
        content, notes = _split_notes(chunk)
        if not content.strip() and not notes:
            continue
        slides.append(
            Slide(
                number=index,
                title=_title(content),
                body=content.strip(),
                notes=notes,
            )
        )
    return slides


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
