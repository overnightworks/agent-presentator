"""The typed private capabilities used by the co-presenter application."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager

    from presentator.contracts.copresenter import (
        AccessRefusal,
        AnswerEvent,
        CoPresenterReadiness,
        DeckText,
        HearingTranscript,
        PersonToken,
        Question,
    )
    from presentator.contracts.decks import Deck, Source


class PrivateHearing(Protocol):
    """One private speech-recognition connection."""

    async def send_pcm(self, frame: bytes) -> None:
        """Send one PCM frame upward."""

    async def receive(self) -> HearingTranscript | None:
        """Receive one transcript, or None when the private end closed."""

    async def aclose(self) -> None:
        """Close the connection and await its owned work."""


class PrivateCoPresenter(Protocol):
    """The narrow protocol Presentator may use to have a question answered."""

    def answer(
        self,
        question: Question,
    ) -> AbstractAsyncContextManager[AsyncIterator[AnswerEvent]]:
        """Open one incremental answer operation."""
        ...


class SpeechHearing(Protocol):
    """The hearing speech offers Presentator over its private socket."""

    async def readiness(self) -> CoPresenterReadiness:
        """Read the readiness facts the browser consumes."""
        ...

    def hear(self, language: str) -> AbstractAsyncContextManager[PrivateHearing]:
        """Open one hearing connection."""
        ...


class DeckTexts(Protocol):
    """Reads the words of a deck's built commit."""

    def text_of(self, deck: Deck, source: Source) -> DeckText | None:
        """The deck's words, or nothing while it has no build or no slides.

        Raises CoPresenterUnavailableError when its mirror cannot be read.
        """
        ...


SentenceBoundaries = Callable[[str], tuple[str, ...]]
"""Splits text into its sentences; the last may still be unfinished."""


class Speaking(Protocol):
    """Speaks one sentence through the private speech service."""

    async def speak(self, text: str, language: str) -> bytes:
        """The sentence as one whole WAV.

        Raises CoPresenterUnavailableError when speech cannot speak it.
        """
        ...


class PersonAccess(Protocol):
    """Gives a signed-in person's fresh access token for the agent service."""

    async def fresh(self, session_id: str) -> PersonToken | AccessRefusal:
        """The session's person's token, or why it has none.

        Raises CoPresenterUnavailableError when the session store or a worker
        thread fails.
        """
        ...
