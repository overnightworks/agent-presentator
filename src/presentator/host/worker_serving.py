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
from pathlib import Path
from typing import Protocol

from presentator.adapters.worker_children import ChildSurroundings
from presentator.contracts.decks import DECK_DIRECTORY
from presentator.host.worker_termination import (
    SWEEP_SECONDS,
    WATCHDOG_SECONDS,
    Watchdog,
)
from presentator.host.worker_wire import (
    KEPT_LIFE_EXITS,
    ReplyPlan,
    WireExit,
    WireRole,
)

logger = logging.getLogger(__name__)

PEER_CREDENTIALS = struct.Struct("3i")
HEADER_SECONDS = 2.0
UPLOAD_SECONDS = 60.0
GATE_SECONDS = 5.0
QUIESCE_SECONDS = 5.0
REPLY_SECONDS = 70.0
# How long a stalled controller is given past the end of the last phase.
CONTROLLER_STALL_GRACE_SECONDS = 5.0


def life_deadline(accepted_at: float, build_timeout_seconds: float) -> float:
    """The monotonic time by which every phase of an accepted life has ended."""
    phases = (
        HEADER_SECONDS,
        UPLOAD_SECONDS,
        GATE_SECONDS,
        QUIESCE_SECONDS,
        REPLY_SECONDS,
        build_timeout_seconds,
    )
    return accepted_at + sum(phases)


def emergency_end(accepted_at: float, build_timeout_seconds: float) -> float:
    """The monotonic time past which only the watchdog acts."""
    return (
        life_deadline(accepted_at, build_timeout_seconds)
        + CONTROLLER_STALL_GRACE_SECONDS
    )


def _phase_end(serving: "Serving", budget: float, life_ends_at: float) -> float:
    """A phase starting now ends after its budget, never past the life's deadline."""
    return min(serving.clock() + budget, life_ends_at)


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
    CANCELLED = "cancelled"


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


class StepSpawn(Protocol):
    """Starts the step launcher behind a gate, in the project, with its own streams."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
        surroundings: ChildSurroundings,
    ) -> WireChild:
        """Return the gated step."""
        ...


# An exit status, the life end a cancelled child is killed for, or None at the end.
WaitForExit = Callable[[WireChild, float], int | LifeEnd | None]


@dataclass(frozen=True)
class StepEffects:
    """What the build step runs through: spawn, gate, paths, environment, place."""

    spawn: StepSpawn
    gate_admits: Callable[[str], bool]
    prepare: Callable[[Path], int]
    environment: Mapping[str, str]
    project: Path


@dataclass(frozen=True)
class Serving:
    """The effects a serving life runs through."""

    spawn: Spawn
    gate_admits: Callable[[str], bool]
    sweep: Callable[[float], bool]
    create_record: Callable[[], int]
    seal: Callable[[int], None]
    open_deck: Callable[[Path], None]
    close_deck: Callable[[Path], None]
    work: Path
    step: StepEffects
    watchdog: Watchdog
    clock: Callable[[], float]
    readable: Callable[[Sequence[int], float], frozenset[int]]
    build_timeout_seconds: float
    build_output_megabytes: int


def _admitted_at_gate(child: WireChild, gate_admits: Callable[[str], bool]) -> bool:
    try:
        return gate_admits(child.status_text())
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


def _command(
    role: WireRole,
    connection: int,
    record: int,
    serving: Serving,
    plan: ReplyPlan | None = None,
) -> list[str]:
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
        "--work",
        str(serving.work),
        "--megabytes",
        str(serving.build_output_megabytes),
        *(["--plan", plan] if plan is not None else []),
    ]


@dataclass(frozen=True)
class _Launch:
    """A gated child's name, its spawn and the gate that judges it."""

    name: str
    spawn: Callable[[], WireChild]
    gate_admits: Callable[[str], bool]


@dataclass(frozen=True)
class _Phase:
    """One gated child's phase: how it starts, ends and is waited on.

    `cancelled_by` is the descriptor whose readiness, at the last check before
    the release, cancels the phase.
    """

    launch: _Launch
    ends_at: float
    expired: LifeEnd | None
    wait: WaitForExit
    cancelled_by: int | None = None


def _wire_launch(
    serving: Serving,
    role: WireRole,
    descriptors: tuple[int, int],
    plan: ReplyPlan | None = None,
) -> _Launch:
    spawn = partial(
        serving.spawn,
        _command(role, *descriptors, serving, plan),
        environment={"PATH": os.environ["PATH"]},
        pass_fds=descriptors,
    )
    return _Launch(role, spawn, serving.gate_admits)


def _run_gated(serving: Serving, phase: _Phase) -> _ChildRun:
    """Spawn behind the gate, release and wait until the phase ends; kill on expiry.

    A phase whose end has already come spawns nothing.
    """
    if serving.clock() >= phase.ends_at:
        logger.warning("%s deadline", phase.launch.name)
        return _ChildRun(None, phase.expired)
    try:
        child = phase.launch.spawn()
    except OSError:
        return _ChildRun(None, LifeEnd.SPAWN)
    try:
        return _released_run(child, serving, phase)
    except BaseException as original:
        _killed_noting_failure(child, original)
        raise
    finally:
        child.close()


def _refused_before_release(
    child: WireChild, serving: Serving, phase: _Phase
) -> _ChildRun | None:
    """Kill the child when its gate, the phase's end or a cancellation says so."""
    if not _admitted_at_gate(child, phase.launch.gate_admits):
        return _ChildRun(None, _killed(child, LifeEnd.GATE))
    if serving.clock() >= phase.ends_at:
        return _ChildRun(None, _expired(child, phase.launch.name, phase.expired))
    if phase.cancelled_by is not None and serving.readable((phase.cancelled_by,), 0.0):
        return _ChildRun(None, _killed(child, LifeEnd.CANCELLED))
    return None


def _released_run(child: WireChild, serving: Serving, phase: _Phase) -> _ChildRun:
    if (refused := _refused_before_release(child, serving, phase)) is not None:
        return refused
    if not _released(child):
        return _ChildRun(None, _killed(child, LifeEnd.GATE))
    outcome = phase.wait(child, phase.ends_at)
    if outcome is None:
        return _ChildRun(None, _expired(child, phase.launch.name, phase.expired))
    if isinstance(outcome, LifeEnd):
        return _ChildRun(None, _killed(child, outcome))
    return _ChildRun(outcome)


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
    serving: Serving,
    listener: socket.socket,
    child: WireChild,
    ends_at: float,
    cancelled_by: int | None = None,
) -> int | LifeEnd | None:
    """Wait for the child's exit until ends_at, closing side connections at accept.

    A readable `cancelled_by` ends the wait with the life end CANCELLED.
    """
    listening = True
    while (remaining := ends_at - serving.clock()) > 0:
        watched = [child.exit_descriptor]
        if listening:
            watched.append(listener.fileno())
        if cancelled_by is not None:
            watched.append(cancelled_by)
        ready = serving.readable(watched, remaining)
        if cancelled_by in ready:
            return LifeEnd.CANCELLED
        if child.exit_descriptor in ready:
            return child.wait()
        if listening and listener.fileno() in ready:
            listening = _side_connection_closed(listener)
    return None


def _expired(child: WireChild, name: str, life_end: LifeEnd | None) -> LifeEnd | None:
    logger.warning("%s deadline", name)
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
        serving.open_deck(serving.work / DECK_DIRECTORY)
    except OSError:
        logger.exception("deck not opened")
        return False
    return True


def _run_upload_with_deck(serving: Serving, phase: _Phase) -> _ChildRun:
    deck = serving.work / DECK_DIRECTORY
    try:
        upload = _run_gated(serving, phase)
    except BaseException as original:
        _noting_cleanup_failure(
            partial(serving.close_deck, deck), original, "closing the deck"
        )
        raise
    serving.close_deck(deck)
    return upload


def _reply_plan(step: _ChildRun, *, quiet: bool) -> ReplyPlan:
    """What the sender answers, from how the step ended and whether it left quiet."""
    if step.exit_status is None:
        return ReplyPlan.FAILED_NULL
    if step.exit_status != 0:
        return ReplyPlan.FAILED_TEXT
    return ReplyPlan.WALK if quiet else ReplyPlan.FAILED_NULL


def _hand_over_reply(
    connection: int,
    serving: Serving,
    launch: _Launch,
    wait: WaitForExit,
    life_ends_at: float,
) -> LifeEnd | None:
    phase = _Phase(
        launch,
        _phase_end(serving, REPLY_SECONDS, life_ends_at),
        LifeEnd.REPLY_EXPIRED,
        wait,
        cancelled_by=connection,
    )
    reply = _run_gated(serving, phase)
    if reply.exit_status is None:
        return reply.life_end
    if reply.exit_status == WireExit.SENT:
        return LifeEnd.CONSUMED
    return LifeEnd.WIRE_EXIT


def _step_command(record: int, serving: Serving) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-m",
        "presentator.host.worker_step",
        "--record-fd",
        str(record),
        "--work",
        str(serving.work),
    ]


def _spawn_step(serving: Serving, record: int) -> WireChild:
    """Make the step's paths, then start it with its stderr in the prepared file."""
    stderr = serving.step.prepare(serving.work)
    try:
        return serving.step.spawn(
            _step_command(record, serving),
            environment=serving.step.environment,
            pass_fds=(record,),
            surroundings=ChildSurroundings(
                cwd=serving.step.project, discard_stdout=True, stderr=stderr
            ),
        )
    finally:
        os.close(stderr)


def _bounded_step_exit(
    serving: Serving,
    listener: socket.socket,
    connection: int,
    life_ends_at: float,
    child: WireChild,
    _gate_ends_at: float,
) -> int | LifeEnd | None:
    """Wait for the step from its release until build_timeout_seconds have passed."""
    ends_at = _phase_end(serving, serving.build_timeout_seconds, life_ends_at)
    return _watched_exit(serving, listener, child, ends_at, connection)


def _hand_over_step(
    connection: int,
    serving: Serving,
    record: int,
    listener: socket.socket,
    life_ends_at: float,
) -> LifeEnd | None:
    """Run the step, then quiesce; only a cancellation ends the life, else the reply."""
    launch = _Launch(
        "step", partial(_spawn_step, serving, record), serving.step.gate_admits
    )
    phase = _Phase(
        launch,
        _phase_end(serving, GATE_SECONDS, life_ends_at),
        None,
        partial(_bounded_step_exit, serving, listener, connection, life_ends_at),
    )
    step = _run_gated(serving, phase)
    if step.life_end is LifeEnd.CANCELLED:
        return LifeEnd.CANCELLED
    quiet = serving.sweep(_phase_end(serving, QUIESCE_SECONDS, life_ends_at))
    plan = _reply_plan(step, quiet=quiet)
    return _hand_over_reply(
        connection,
        serving,
        _wire_launch(serving, WireRole.SEND, (connection, record), plan),
        partial(_watched_exit, serving, listener),
        life_ends_at,
    )


def _hand_over_upload(
    descriptors: tuple[int, int],
    serving: Serving,
    listener: socket.socket,
    upload_ends_at: float,
    life_ends_at: float,
) -> LifeEnd | None:
    connection, record = descriptors
    if not _sealed(serving, record):
        return LifeEnd.SEAL_FAILED
    if not _opened_deck(serving):
        return LifeEnd.DECK_FAILED
    phase = _Phase(
        _wire_launch(serving, WireRole.UPLOAD, (connection, record)),
        upload_ends_at,
        LifeEnd.UPLOAD_EXPIRED,
        partial(_watched_exit, serving, listener),
    )
    upload = _run_upload_with_deck(serving, phase)
    if upload.exit_status is None:
        return upload.life_end
    if upload.exit_status == WireExit.UPLOADED:
        return _hand_over_step(connection, serving, record, listener, life_ends_at)
    return LifeEnd.WIRE_EXIT


def _hand_over_with_record(
    connection: int,
    serving: Serving,
    accepted_at: float,
    record: int,
    listener: socket.socket,
) -> LifeEnd | None:
    header_phase = _Phase(
        _wire_launch(serving, WireRole.IDLE, (connection, record)),
        accepted_at + HEADER_SECONDS,
        None,
        partial(_waited_within, serving),
    )
    header = _run_gated(serving, header_phase)
    if header.exit_status is None:
        return header.life_end
    if header.exit_status != WireExit.ACCEPTED:
        return None if header.exit_status in KEPT_LIFE_EXITS else LifeEnd.WIRE_EXIT
    life_ends_at = life_deadline(accepted_at, serving.build_timeout_seconds)
    upload_ends_at = _phase_end(serving, UPLOAD_SECONDS, life_ends_at)
    serving.watchdog.arm(
        emergency_end(accepted_at, serving.build_timeout_seconds) - serving.clock()
    )
    return _hand_over_upload(
        (connection, record), serving, listener, upload_ends_at, life_ends_at
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
