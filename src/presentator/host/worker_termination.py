"""How a worker life ends: the sweep to PID 1 and the watchdog that bounds a stall."""

import faulthandler
import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

SWEEP_SECONDS = 4.0
WATCHDOG_SECONDS = 7.0
SWEEP_INTERVAL_SECONDS = 0.05
PID_ONE = "1"
OWN_STDERR = "/proc/self/fd/2"


@dataclass(frozen=True)
class SweepEffects:
    """What one sweep round does to the process table."""

    kill_all: Callable[[], None]
    reap: Callable[[], None]
    list_process_ids: Callable[[], Sequence[str]]
    clock: Callable[[], float]
    sleep: Callable[[float], None]


def sweep(effects: SweepEffects, deadline: float) -> bool:
    """Kill, reap and list every 50 ms until only PID 1 is left or time is up."""
    while True:
        with suppress(ProcessLookupError):  # no target is left to signal
            effects.kill_all()
        effects.reap()
        try:
            process_ids = effects.list_process_ids()
        except OSError:
            return False
        if set(process_ids) == {PID_ONE}:
            return True
        remaining = deadline - effects.clock()
        if remaining <= 0:
            return False
        effects.sleep(min(SWEEP_INTERVAL_SECONDS, remaining))


def reap_children() -> None:
    """Collect every exited child without waiting."""
    with suppress(ChildProcessError):
        reaped = True
        while reaped:
            reaped = os.waitpid(-1, os.WNOHANG)[0] != 0


def list_process_ids(proc_root: Path) -> list[str]:
    """Name the numeric entries under a /proc root."""
    return [entry.name for entry in proc_root.iterdir() if entry.name.isdecimal()]


class Watchdog(Protocol):
    """Ends the process when it stalls; a second arm replaces the first."""

    def arm(self, remaining: float) -> None:
        """Exit with status 1 after the given seconds."""
        ...

    def cancel(self) -> None:
        """Disarm the pending exit."""
        ...


class FaultHandlerWatchdog:
    """The faulthandler timer: a C thread that needs no interpreter progress."""

    def __init__(self) -> None:
        """Open nothing until the first arm."""
        self._stderr: IO[str] | None = None

    def arm(self, remaining: float) -> None:
        """Exit with status 1 after the given seconds, writing without blocking."""
        if self._stderr is None:
            descriptor = os.open(OWN_STDERR, os.O_WRONLY | os.O_NONBLOCK)
            self._stderr = os.fdopen(descriptor, "w")
        faulthandler.dump_traceback_later(remaining, exit=True, file=self._stderr)

    def cancel(self) -> None:
        """Disarm the pending exit."""
        faulthandler.cancel_dump_traceback_later()
