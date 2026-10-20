"""Chores that run on a schedule, so no page view has to.

Polling the deck sources is the guarantee that a push arrives; the fetch hook
only makes it faster (ADR 0010). The use cases know nothing of it: a schedule is
handed a chore to call, and the server owns when it runs.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Schedule:
    """Runs a chore once every interval, on a task beside the routes."""

    chore: Callable[[], object]
    interval: timedelta

    @asynccontextmanager
    async def polling(self) -> AsyncGenerator[None]:
        """Run the chore for exactly as long as the block it wraps runs."""
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
        """One run of the chore, in a thread, so a slow one holds up no answer."""
        await asyncio.to_thread(self.chore)

    async def _every_interval(self) -> None:
        while True:
            await asyncio.sleep(self.interval.total_seconds())
            # Awaiting the whole tick is what keeps two of them from running at
            # once, and the fetch's own bound is what ends the one that hangs.
            try:
                await self.tick()
            except Exception:
                # The next interval is the retry; ending the loop would stop
                # the chore for good without a word.
                _logger.exception(
                    "A scheduled chore failed; it runs again next interval"
                )
