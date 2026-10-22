"""The words of a deck, read from its built commit in the source's mirror.

Slidev owns the real parser. This reader is the honest subset a voice needs:
Slidev's `---` separators and per-slide frontmatter, a heading as the title,
the last HTML comment as speaker notes. It is not a second slide map.
"""

import re
from dataclasses import dataclass
from typing import Final

from gitmirror.model import MirrorError, Revision
from presentator.adapters.deck_manifest import (
    DeckManifest,
    MalformedManifestError,
    read_manifest,
)
from presentator.adapters.decks import SourceMirrors
from presentator.contracts.copresenter import (
    CoPresenterUnavailableError,
    DeckText,
    SlideText,
)
from presentator.contracts.decks import MANIFEST_FILE, SLIDES_FILE, Deck, Source

_SEPARATOR: Final = "---"
_FENCE_OPEN = re.compile(r"^\s*`+")
_NOTE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_TITLE_LINE = re.compile(r"^#\s+(\S.*)$", re.MULTILINE)
_NO_MANIFEST = DeckManifest(title=None, language=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class MirroredDeckTexts:
    """Reads a deck's words out of the mirror of the source it came from."""

    mirrors: SourceMirrors

    def text_of(self, deck: Deck, source: Source) -> DeckText | None:
        """The deck's title, language and slides at its built commit.

        Nothing while the deck has no build or its commit carries no slides;
        a mirror that cannot be read is unavailable.
        """
        if deck.build is None:
            return None
        mirror = self.mirrors.of(source)
        if mirror is None:
            raise CoPresenterUnavailableError
        revision = Revision(ref=source.ref, commit=deck.build.commit)
        try:
            names = {entry.name for entry in mirror.entries(revision, inside=deck.slug)}
            if SLIDES_FILE not in names:
                return None
            markdown = mirror.read(revision, f"{deck.slug}/{SLIDES_FILE}").decode()
            manifest_bytes = (
                mirror.read(revision, f"{deck.slug}/{MANIFEST_FILE}")
                if MANIFEST_FILE in names
                else None
            )
        except (MirrorError, UnicodeDecodeError) as unreadable:
            raise CoPresenterUnavailableError from unreadable
        slides = _slides(markdown)
        manifest = _manifest(manifest_bytes, folder=deck.slug)
        return DeckText(
            title=manifest.title or (slides[0].title if slides else deck.slug),
            language=manifest.language,
            slides=slides,
        )


def _manifest(manifest: bytes | None, *, folder: str) -> DeckManifest:
    if manifest is None:
        return _NO_MANIFEST
    try:
        return read_manifest(manifest, folder=folder)
    except MalformedManifestError:
        return _NO_MANIFEST


def _slides(markdown: str) -> tuple[SlideText, ...]:
    slides: list[SlideText] = []
    number = 1
    for chunk in _slide_chunks(markdown):
        content, notes = _split_notes(_without_frontmatter(chunk))
        if not content.strip() and not notes:
            continue
        slides.append(
            SlideText(
                number=number,
                title=_title(content),
                body=content.strip(),
                notes=notes,
            )
        )
        number += 1
    return tuple(slides)


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
            start = index if opens_frontmatter else index + 1
            index = _after_separator(lines, index, opens_frontmatter=opens_frontmatter)
            continue
        marker = _code_fence_marker(line)
        if marker is not None:
            index = _after_code_fence(lines, index + 1, marker)
            continue
        in_html_comment = _html_comment_continues(raw_line, inside=False)
        index += 1
    if start < length:
        _collect_chunk(chunks, lines, start, length)
    return chunks


def _after_separator(lines: list[str], index: int, *, opens_frontmatter: bool) -> int:
    if not opens_frontmatter:
        return index + 1
    index += 1
    while index < len(lines) and lines[index].rstrip() != _SEPARATOR:
        index += 1
    return index + 1


def _code_fence_marker(line: str) -> str | None:
    fence = _FENCE_OPEN.match(line)
    if fence is None or not line.lstrip().startswith("```"):
        return None
    return fence.group(0)


def _after_code_fence(lines: list[str], index: int, marker: str) -> int:
    while index < len(lines) and not lines[index].startswith(marker):
        index += 1
    return min(index + 1, len(lines))


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
