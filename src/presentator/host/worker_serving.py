"""The build worker's serving loop: accept, check the peer, hand over, sweep."""

import logging
import os
import socket
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Protocol

from presentator.host.worker_termination import (
    SWEEP_SECONDS,
    WATCHDOG_SECONDS,
    Watchdog,
)
from presentator.host.worker_wire import KEPT_LIFE_EXITS, WireExit, WireRole

logger = logging.getLogger(__name__)

PEER_CREDENTIALS = struct.Struct("3i")
HEADER_SECONDS = 2.0
UPLOAD_SECONDS = 60.0
GATE_SECONDS = 5.0
QUIESCE_SECONDS = 5.0
REPLY_SECONDS = 70.0
# How long a stalled controller is given past the end of the last phase.
CONTROLLER_STALL_GRACE_SECONDS = 5.0


def emergency_end(accepted_at: float, build_timeout_seconds: float) -> float:
    """The monotonic time past which only the watchdog acts."""
    phases = (
        HEADER_SECONDS,
        UPLOAD_SECONDS,
        GATE_SECONDS,
        QUIESCE_SECONDS,
        REPLY_SECONDS,
        build_timeout_seconds,
        CONTROLLER_STALL_GRACE_SECONDS,
    )
    return accepted_at + sum(phases)


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
    CONSUMED = "consumed"
    WIRE_EXIT = "wire-exit"
    SWEEP = "sweep"
    ACCEPT = "accept"
    SEAL_FAILED = "seal-failed"
    UPLOAD_EXPIRED = "upload-expired"
    REPLY_EXPIRED = "reply-expired"
    DECK_FAILED = "deck-failed"


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

    @property
    def exit_descriptor(self) -> int:
        """A descriptor that becomes readable when the child has exited."""
        ...

    def close(self) -> None:
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


WaitForExit = Callable[[WireChild, float], int | None]


@dataclass(frozen=True)
class Serving:
    """The effects a serving life runs through."""

    spawn: Spawn
    gate_admits: Callable[[str], bool]
    sweep: Callable[[float], bool]
    create_record: Callable[[], int]
    seal: Callable[[int], None]
    open_deck: Callable[[], None]
    close_deck: Callable[[], None]
    watchdog: Watchdog
    clock: Callable[[], float]
    readable: Callable[[Sequence[int], float], frozenset[int]]
    build_timeout_seconds: float


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
    try:
        child.kill()
    finally:
        child.wait()
    return life_end


def _noting_cleanup_failure(
    cleanup: Callable[[], object], original: BaseException, what: str
) -> None:
    try:
        cleanup()
    except (Exception, KeyboardInterrupt) as cleanup_failure:
        logger.exception("%s failed", what)
        original.add_note(f"{what} failed: {cleanup_failure!r}")


def _killed_noting_failure(child: WireChild, original: BaseException) -> None:
    _noting_cleanup_failure(
        partial(_killed, child, None), original, "killing and reaping the child"
    )


@dataclass(frozen=True)
class _ChildRun:
    """How a gated child's run ended: its exit status, else the life end to take."""

    exit_status: int | None
    life_end: LifeEnd | None = None


def _command(role: WireRole, connection: int, record: int) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-m",
        "presentator.host.worker_wire",
        "--role",
        role,
        "--connection-fd",
        str(connection),
        "--record-fd",
        str(record),
    ]


@dataclass(frozen=True)
class _Phase:
    """One wire child's phase: its role, its absolute end and how it is waited on."""

    role: WireRole
    ends_at: float
    expired: LifeEnd | None
    wait: WaitForExit


def _run_gated(
    serving: Serving, phase: _Phase, descriptors: tuple[int, int]
) -> _ChildRun:
    """Spawn behind the gate, release and wait until the phase ends; kill on expiry."""
    try:
        child = serving.spawn(
            _command(phase.role, *descriptors),
            environment={"PATH": os.environ["PATH"]},
            pass_fds=descriptors,
        )
    except OSError:
        return _ChildRun(None, LifeEnd.SPAWN)
    try:
        return _released_run(child, serving, phase)
    except BaseException as original:
        _killed_noting_failure(child, original)
        raise
    finally:
        child.close()


def _released_run(child: WireChild, serving: Serving, phase: _Phase) -> _ChildRun:
    if not _admitted_at_gate(child, serving):
        return _ChildRun(None, _killed(child, LifeEnd.GATE))
    if serving.clock() >= phase.ends_at:
        return _ChildRun(None, _expired(child, phase.role, phase.expired))
    if not _released(child):
        return _ChildRun(None, _killed(child, LifeEnd.GATE))
    exit_status = phase.wait(child, phase.ends_at)
    if exit_status is None:
        return _ChildRun(None, _expired(child, phase.role, phase.expired))
    return _ChildRun(exit_status)


def _waited_within(serving: Serving, child: WireChild, ends_at: float) -> int | None:
    remaining = ends_at - serving.clock()
    return child.wait_within(remaining) if remaining > 0 else None


def _side_connection_closed(listener: socket.socket) -> bool:
    """Accept and close one waiting connection; False when accepting failed."""
    listener.settimeout(0.0)
    try:
        side_connection, _address = listener.accept()
    except BlockingIOError:
        return True
    except OSError:
        logger.exception("side connection accept failed")
        return False
    else:
        with side_connection:
            return True
    finally:
        listener.settimeout(None)


def _watched_exit(
    serving: Serving, listener: socket.socket, child: WireChild, ends_at: float
) -> int | None:
    """Wait for the child's exit until ends_at, closing side connections at accept."""
    listening = True
    while (remaining := ends_at - serving.clock()) > 0:
        watched = [child.exit_descriptor]
        if listening:
            watched.append(listener.fileno())
        ready = serving.readable(watched, remaining)
        if child.exit_descriptor in ready:
            return child.wait()
        if listening and listener.fileno() in ready:
            listening = _side_connection_closed(listener)
    return None


def _expired(
    child: WireChild, role: WireRole, life_end: LifeEnd | None
) -> LifeEnd | None:
    logger.warning("%s deadline", role)
    return _killed(child, life_end)


def _sealed(serving: Serving, record: int) -> bool:
    try:
        serving.seal(record)
    except OSError:
        logger.exception("request record not sealed")
        return False
    return True


def _opened_deck(serving: Serving) -> bool:
    try:
        serving.open_deck()
    except OSError:
        logger.exception("deck not opened")
        return False
    return True


def _run_upload_with_deck(
    serving: Serving, descriptors: tuple[int, int], phase: _Phase
) -> _ChildRun:
    try:
        upload = _run_gated(serving, phase, descriptors)
    except BaseException as original:
        _noting_cleanup_failure(serving.close_deck, original, "closing the deck")
        raise
    serving.close_deck()
    return upload


def _hand_over_reply(
    connection: int, serving: Serving, record: int, wait: WaitForExit
) -> LifeEnd | None:
    phase = _Phase(
        WireRole.SEND, serving.clock() + REPLY_SECONDS, LifeEnd.REPLY_EXPIRED, wait
    )
    reply = _run_gated(serving, phase, (connection, record))
    if reply.exit_status is None:
        return reply.life_end
    if reply.exit_status == WireExit.SENT:
        return LifeEnd.CONSUMED
    return LifeEnd.WIRE_EXIT


def _hand_over_upload(
    connection: int,
    serving: Serving,
    record: int,
    upload_ends_at: float,
    wait: WaitForExit,
) -> LifeEnd | None:
    if not _sealed(serving, record):
        return LifeEnd.SEAL_FAILED
    if not _opened_deck(serving):
        return LifeEnd.DECK_FAILED
    phase = _Phase(WireRole.UPLOAD, upload_ends_at, LifeEnd.UPLOAD_EXPIRED, wait)
    upload = _run_upload_with_deck(serving, (connection, record), phase)
    if upload.exit_status is None:
        return upload.life_end
    if upload.exit_status == WireExit.UPLOADED:
        return _hand_over_reply(connection, serving, record, wait)
    return LifeEnd.WIRE_EXIT


def _hand_over_with_record(
    connection: int,
    serving: Serving,
    accepted_at: float,
    record: int,
    listener: socket.socket,
) -> LifeEnd | None:
    header_phase = _Phase(
        WireRole.IDLE,
        accepted_at + HEADER_SECONDS,
        None,
        partial(_waited_within, serving),
    )
    header = _run_gated(serving, header_phase, (connection, record))
    if header.exit_status is None:
        return header.life_end
    if header.exit_status != WireExit.ACCEPTED:
        return None if header.exit_status in KEPT_LIFE_EXITS else LifeEnd.WIRE_EXIT
    upload_ends_at = serving.clock() + UPLOAD_SECONDS
    serving.watchdog.arm(
        emergency_end(accepted_at, serving.build_timeout_seconds) - serving.clock()
    )
    return _hand_over_upload(
        connection,
        serving,
        record,
        upload_ends_at,
        partial(_watched_exit, serving, listener),
    )


def hand_over(
    connection: socket.socket,
    serving: Serving,
    accepted_at: float,
    *,
    listener: socket.socket,
) -> LifeEnd | None:
    """Give the connection to gated wire children; None keeps the life."""
    record = serving.create_record()
    try:
        return _hand_over_with_record(
            connection.fileno(), serving, accepted_at, record, listener
        )
    finally:
        os.close(record)


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
                hand_over(connection, serving, accepted_at, listener=listener)
                if admitted
                else None
            )
        if life_end is not None:
            return life_end
        if not serving.sweep(accepted_at + SWEEP_SECONDS):
            return LifeEnd.SWEEP
        serving.watchdog.cancel()
