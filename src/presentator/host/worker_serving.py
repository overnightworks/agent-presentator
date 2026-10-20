"""The build worker's serving loop: accept, check the peer, hand over, sweep."""

import logging
import os
import socket
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from presentator.host.worker_termination import (
    SWEEP_SECONDS,
    WATCHDOG_SECONDS,
    Watchdog,
)
from presentator.host.worker_wire import KEPT_LIFE_EXITS

logger = logging.getLogger(__name__)

PEER_CREDENTIALS = struct.Struct("3i")
HEADER_SECONDS = 2.0


def peer_admitted(peer_uid: int, runtime_uid: int) -> bool:
    """Only the runtime uid may talk to the worker; uid 0 never."""
    return peer_uid != 0 and peer_uid == runtime_uid


def _peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, PEER_CREDENTIALS.size
    )
    _pid, uid, _gid = PEER_CREDENTIALS.unpack(credentials)
    return uid


class LifeEnd(StrEnum):
    """What ended a serving life, named for the log."""

    SPAWN = "spawn"
    GATE = "gate"
    WIRE_EXIT = "wire-exit"
    SWEEP = "sweep"
    ACCEPT = "accept"


class WireChild(Protocol):
    """A spawned wire child that waits at its gate."""

    def status_text(self) -> str:
        """Read the kernel's account of the child."""
        ...

    def release(self) -> None:
        """Let the child's program run."""
        ...

    def kill(self) -> None:
        """Kill the child."""
        ...

    def wait(self) -> int:
        """Wait for the child and return its exit status."""
        ...

    def wait_within(self, seconds: float) -> int | None:
        """Wait at most the seconds; None when the child is still running."""
        ...

    def close_gate(self) -> None:
        """Close the controller's end of the gate."""
        ...


class Spawn(Protocol):
    """Starts a command behind a gate with the given descriptors kept open."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> WireChild:
        """Return the gated child."""
        ...


@dataclass(frozen=True)
class Serving:
    """The effects a serving life runs through."""

    spawn: Spawn
    gate_admits: Callable[[str], bool]
    sweep: Callable[[float], bool]
    watchdog: Watchdog
    clock: Callable[[], float]


def _admitted_at_gate(child: WireChild, serving: Serving) -> bool:
    try:
        return serving.gate_admits(child.status_text())
    except OSError:
        return False


def _released(child: WireChild) -> bool:
    try:
        child.release()
    except OSError:
        return False
    return True


def _killed(child: WireChild, life_end: LifeEnd | None) -> LifeEnd | None:
    child.kill()
    child.wait()
    return life_end


def _header_expired(child: WireChild) -> None:
    logger.warning("header deadline")
    _killed(child, None)


def hand_over(
    connection: socket.socket, serving: Serving, header_ends_at: float
) -> LifeEnd | None:
    """Give the connection to a gated wire child; None keeps the life."""
    descriptor = connection.fileno()
    command = [
        sys.executable,
        "-I",
        "-m",
        "presentator.host.worker_wire",
        "--connection-fd",
        str(descriptor),
    ]
    try:
        child = serving.spawn(
            command, environment={"PATH": os.environ["PATH"]}, pass_fds=(descriptor,)
        )
    except OSError:
        return LifeEnd.SPAWN
    try:
        if not _admitted_at_gate(child, serving):
            return _killed(child, LifeEnd.GATE)
        if serving.clock() >= header_ends_at:
            return _header_expired(child)
        if not _released(child):
            return _killed(child, LifeEnd.GATE)
        remaining = header_ends_at - serving.clock()
        exit_status = child.wait_within(remaining) if remaining > 0 else None
        if exit_status is None:
            return _header_expired(child)
        return None if exit_status in KEPT_LIFE_EXITS else LifeEnd.WIRE_EXIT
    finally:
        child.close_gate()


def accept_connections(
    listener: socket.socket, runtime_uid: int, serving: Serving
) -> LifeEnd:
    """Serve each connection in turn; return what ended the life."""
    while True:
        try:
            connection, _address = listener.accept()
        except OSError:
            logger.exception("accept failed")
            return LifeEnd.ACCEPT
        accepted_at = serving.clock()
        serving.watchdog.arm(accepted_at + WATCHDOG_SECONDS - serving.clock())
        with connection:
            try:
                peer_uid = _peer_uid(connection)
            except OSError:
                logger.exception("peer credentials unreadable")
                return LifeEnd.ACCEPT
            admitted = peer_admitted(peer_uid, runtime_uid)
            life_end = (
                hand_over(connection, serving, accepted_at + HEADER_SECONDS)
                if admitted
                else None
            )
        if life_end is not None:
            return life_end
        if not serving.sweep(accepted_at + SWEEP_SECONDS):
            return LifeEnd.SWEEP
        serving.watchdog.cancel()
