"""The typed private capabilities used by the co-presenter application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager

    from presentator.contracts.copresenter import (
        AnswerEvent,
        CoPresenterReadiness,
        HearingTranscript,
        HearingUnavailable,
        Question,
    )


class PrivateHearing(Protocol):
    """One private speech-recognition connection."""

    async def send_pcm(self, frame: bytes) -> None:
        """Send one PCM frame upward."""

    async def receive(self) -> HearingTranscript | HearingUnavailable | None:
        """Receive one transcript, expected failure, or clean end."""

    async def aclose(self) -> None:
        """Close the connection and await its owned work."""


class PrivateCoPresenter(Protocol):
    """The narrow protocol Presentator may use over its private transport."""

    async def readiness(self) -> CoPresenterReadiness:
        """Read the three readiness facts the browser consumes."""
        ...

    def answer(
        self,
        question: Question,
    ) -> AbstractAsyncContextManager[AsyncIterator[AnswerEvent]]:
        """Open one incremental answer operation."""
        ...

    def hear(self, language: str) -> AbstractAsyncContextManager[PrivateHearing]:
        """Open one private hearing child."""
        ...
