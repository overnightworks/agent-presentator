"""Turn what was said on a slide into spoken sentences."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from agent_providers.claude.adapter import stream_claude_turn
from agent_providers.config import (
    ClaudeCliRuntime,
    ProviderRuntimeConfig,
    TurnRuntimeConfig,
    configure,
    current_claude_runtime,
)
from agent_providers.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_providers.events import AssistantTextEvent

from copresenter.deck import Deck, Slide
from copresenter.sentences import flush, take_sentences
from copresenter.speech import Speech

SYSTEM_PROMPT = (
    "You are the co-presenter on stage with the speaker. "
    "Answer in the language of the question; default to German. "
    "Speak as spoken text: no markdown, no emoji, no bullet lists, "
    "no navigation commands, no stage directions. "
    "Two or three short sentences. "
    "Base the answer on the current slide and the rest of the deck. "
    "If the question is off-topic, say so briefly and return to the slide. "
    "Use 'KI' not 'AI' when speaking German."
)

_CLAUDE_CLI_BINARY = "claude"
_CLAUDE_TURN_USER = "copresenter"
_log = logging.getLogger("copresenter")


class Answerer(Protocol):
    """A streaming text source. Production is Claude; tests inject a stand-in."""

    model: str
    provider: str

    def stream(self, *, said: str, slide: int, deck: Deck) -> AsyncIterator[str]:
        """Yield text deltas for this utterance."""
        ...


def user_message(said: str, slide_number: int, deck: Deck) -> str:
    """The Claude turn: what was said, the current slide, the deck around it."""
    pages = "\n\n".join(_page(slide) for slide in deck.slides)
    try:
        current = deck.at(slide_number)
        here = _page(current, current_mark=True)
    except KeyError:
        here = f"Folie {slide_number} is not in this deck."
    return (
        f"Deck: {deck.title}\n"
        f"You are on slide {slide_number}.\n\n"
        f"--- CURRENT SLIDE ---\n{here}\n--- END CURRENT SLIDE ---\n\n"
        f"--- DECK ---\n{pages}\n--- END DECK ---\n\n"
        f"The person said: {said}"
    )


def _page(slide: Slide, *, current_mark: bool = False) -> str:
    mark = " (current)" if current_mark else ""
    notes = f"\nSpeaker notes: {slide.notes}" if slide.notes else ""
    return f"Slide {slide.number}{mark}: {slide.title}\n{slide.body}{notes}"


def provider_runtime(
    *,
    token_file: Path,
    search_path: tuple[Path, ...],
    working_root: Path,
) -> ProviderRuntimeConfig:
    """The agent-providers runtime this process installs once.

    Answering is the `claude` CLI found only on `search_path`. Its login is the
    setup token in `token_file`, which the library hands the child as
    `CLAUDE_CODE_OAUTH_TOKEN` and nowhere else. Each turn gets a fresh home
    below `working_root`, which must already be a private directory.
    """
    return ProviderRuntimeConfig(
        claude=ClaudeCliRuntime(binary=_CLAUDE_CLI_BINARY, token_file=token_file),
        cli_binary_search_path=search_path,
        cli_working_directory_root=working_root,
        turns=TurnRuntimeConfig(mcp_server=None),
    )


class ClaudeAnswerer:
    """One Claude turn through the installed CLI, streaming text deltas."""

    def __init__(self, *, model: str, runtime: ProviderRuntimeConfig) -> None:
        """Install the provider runtime once. No API key is stored."""
        self.provider = "claude"
        self.model = model
        configure(runtime)

    async def stream(self, *, said: str, slide: int, deck: Deck) -> AsyncIterator[str]:
        """Yield Claude's text as it arrives."""
        try:
            async for event in stream_claude_turn(
                user_id=_CLAUDE_TURN_USER,
                system=SYSTEM_PROMPT,
                model=self.model,
                messages=[{"role": "user", "content": user_message(said, slide, deck)}],
            ):
                if isinstance(event, AssistantTextEvent) and event.text:
                    yield event.text
        except ProviderUnavailableError as unavailable:
            if (
                unavailable.reason is not None
                and unavailable.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED
            ):
                _log.error(  # the path only, never the file's content
                    "Claude login file missing or refused: %s",
                    current_claude_runtime().token_file,
                )
            raise


class CannedAnswerer:
    """A deck-aware stand-in used by tests and the browser proof."""

    provider = "canned"
    model = "canned"

    async def stream(self, *, said: str, slide: int, deck: Deck) -> AsyncIterator[str]:
        """Yield two sentences built from the current slide, in small chunks."""
        heard = said.strip().rstrip(".!?")
        try:
            current = deck.at(slide)
        except KeyError:
            text = f"Du hast gesagt: {heard}. Folie {slide} kenne ich in diesem Deck nicht."
        else:
            notes = (current.notes or current.title).rstrip(".")
            text = (
                f"Du hast gesagt: {heard}. "
                f"Wir sind auf Folie {slide}, {current.title}. "
                f"Die Notizen sagen: {notes}."
            )
        for chunk in _chunks(text):
            yield chunk


def _chunks(text: str) -> list[str]:
    words = text.split(" ")
    return [word if index == 0 else f" {word}" for index, word in enumerate(words)]


async def stream_spoken(
    *,
    said: str,
    slide: int,
    deck: Deck,
    answerer: Answerer,
    speech: Speech,
    language: str,
) -> AsyncIterator[tuple[str, dict[str, object]]]:
    """Stream text deltas, then each finished sentence as spoken WAV."""
    full: list[str] = []
    buffer = ""
    async for delta in answerer.stream(said=said, slide=slide, deck=deck):
        yield ("text", {"text": delta})
        buffer += delta
        sentences, buffer = take_sentences(buffer)
        async for spoken in _speak_each(sentences, speech, language, full):
            yield spoken
    leftover = flush(buffer)
    if leftover:
        async for spoken in _speak_each([leftover], speech, language, full):
            yield spoken
    yield ("done", {"text": " ".join(full)})


async def _speak_each(
    sentences: list[str],
    speech: Speech,
    language: str,
    full: list[str],
) -> AsyncIterator[tuple[str, dict[str, object]]]:
    for sentence in sentences:
        full.append(sentence)
        yield ("sentence", {"text": sentence})
        wav = await speech.speak(sentence, language)
        yield ("audio", {"text": sentence, "wav_b64": _b64(wav)})


def _b64(wav: bytes) -> str:
    return base64.standard_b64encode(wav).decode("ascii")
