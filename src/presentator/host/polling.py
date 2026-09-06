"""Fetching the deck sources on a schedule, so no page view has to.

Polling is the guarantee that a push arrives; the fetch hook only makes it
faster (ADR 0010). The use cases know nothing of it: this is handed a refresh
to call, and the server owns when it runs.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePoller:
    """Refreshes the decks once every interval, on a task beside the routes."""

    refresh: Callable[[], None]
    interval: timedelta

    @asynccontextmanager
    async def polling(self) -> AsyncGenerator[None]:
        """Poll for exactly as long as the block it wraps runs."""
        ticking = asyncio.create_task(self._every_interval())
        try:
            yield
        finally:
            ticking.cancel()
            # A cancelled tick leaves its fetch running in its own thread, so
            # the fetch's own bound is what keeps a shutdown finite.
            with suppress(asyncio.CancelledError):
                await ticking

    async def tick(self) -> None:
        """One refresh, in a thread, so a slow fetch holds up no answer."""
        await asyncio.to_thread(self.refresh)

    async def _every_interval(self) -> None:
        while True:
            await asyncio.sleep(self.interval.total_seconds())
            # Awaiting the whole tick is what keeps two of them from running at
            # once, and the fetch's own bound is what ends the one that hangs.
            await self.tick()
