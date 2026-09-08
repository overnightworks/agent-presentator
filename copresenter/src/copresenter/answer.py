"""Turn what was said on a slide into spoken sentences."""

from __future__ import annotations

import base64
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from agent_providers.claude.adapter import stream_claude_turn
from agent_providers.config import ProviderRuntimeConfig, configure
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
_CODEX_MAX_CONCURRENT_PROCESSES = 1
_CODEX_MAX_CONCURRENT_IMAGE_RUNS = 1
_SCRUBBED_SECRET_KEYS = ("ANTHROPIC_API_KEY",)


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


def provider_runtime(model: str) -> ProviderRuntimeConfig:
    """The agent-providers runtime this process installs once.

    Answering is the installed `claude` CLI and the operator's login. The
    optional API key stays empty. Codex caps are required by the config shape
    and unused here, so each bound is 1. Secret names are still scrubbed from
    the child environment.
    """
    unused = Path(tempfile.gettempdir()) / "copresenter-unused"
    unused.mkdir(parents=True, exist_ok=True)
    return ProviderRuntimeConfig(
        claude_chat_model=model,
        anthropic_api_key=None,
        claude_cli_binary=_CLAUDE_CLI_BINARY,
        grok_cli_binary="grok",
        codex_cli_binary="codex",
        grok_cli_auth_file=unused / "grok-auth",
        grok_cli_session_root=unused,
        codex_cli_auth_file=unused / "codex-auth",
        codex_code_mode_host_binary=unused / "codex-host",
        codex_resources_directory=unused,
        codex_max_concurrent_processes=_CODEX_MAX_CONCURRENT_PROCESSES,
        codex_max_concurrent_image_runs=_CODEX_MAX_CONCURRENT_IMAGE_RUNS,
        cli_working_directory_root=unused,
        cli_prompt_file_prefix="copresenter-prompt-",
        cli_prompt_file_placeholder="PROMPT_FILE",
        secret_env_keys=_SCRUBBED_SECRET_KEYS,
        mcp_server=None,
    )


class ClaudeAnswerer:
    """One Claude turn through the installed CLI, streaming text deltas."""

    def __init__(self, *, model: str) -> None:
        """Install the provider runtime once. No API key is stored."""
        self.provider = "claude"
        self.model = model
        configure(provider_runtime(model))

    async def stream(self, *, said: str, slide: int, deck: Deck) -> AsyncIterator[str]:
        """Yield Claude's text as it arrives."""
        async for event in stream_claude_turn(
            user_id=_CLAUDE_TURN_USER,
            system=SYSTEM_PROMPT,
            model=self.model,
            messages=[{"role": "user", "content": user_message(said, slide, deck)}],
        ):
            if isinstance(event, AssistantTextEvent) and event.text:
                yield event.text


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
