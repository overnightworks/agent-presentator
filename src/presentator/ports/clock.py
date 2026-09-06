"""Time enters the product here, so a test can freeze it."""

from abc import abstractmethod
from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Callers ask for the time instead of reading a system clock themselves."""

    @abstractmethod
    def now(self) -> datetime:
        """The current moment, timezone-aware."""
