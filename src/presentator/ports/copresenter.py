"""The typed private capabilities used by the co-presenter application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager

    from presentator.contracts.copresenter import (
        AnswerEvent,
        CoPresenterReadiness,
        DeckText,
        HearingTranscript,
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
