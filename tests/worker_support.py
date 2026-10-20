"""Shared arrangement for the build worker's socket and serving-loop tests."""

import os
import socket
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from presentator.host import worker
from presentator.host.worker_serving import Serving
from presentator.host.worker_wire import WireExit

OWN_UID = os.getuid()


OWN_GID = os.getgid()


FOREIGN_UID = OWN_UID + 1


PRIVATE_MODE = 0o700


CONNECTIONS = 2


WAIT_SECONDS = 5


class StopServingError(Exception):
    """Ends an accept loop the test bounds."""


GATED_STATUS = "Uid:\t10002\t10002\t10002\t10002"


ACCEPT_TIME = 100.0


ADMITTING_PATH = "/opt/test/bin"


@dataclass(frozen=True)
class Spawned:
    """What the controller asked a spawn for."""

    command: Sequence[str]
    environment: Mapping[str, str]
    pass_fds: Sequence[int]


@dataclass
class ScriptedLife:
    """One serving life's effects and its wire child, recorded in order."""

    events: list[str] = field(default_factory=list[str])
    spawn_error: OSError | None = None
    status: str | OSError = GATED_STATUS
    gate_admits: bool = True
    release_error: OSError | None = None
    exit_status: int = WireExit.CLOSED
    headers_stalled: int = 0
    clock_now: float = ACCEPT_TIME
    inspection_takes: float = 0.0
    release_takes: float = 0.0
    budgets_waited: list[float] = field(default_factory=list[float])
    sweep_error: OSError | None = None
    answer: bytes = b""
    sweep_quiet: bool = True
    connections_served_before_sweep_fails: int | None = None
    spawned: list[Spawned] = field(default_factory=list[Spawned])

    def spawn(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> "ScriptedLife":
        self.spawned.append(Spawned(command, environment, pass_fds))
        self.events.append("spawn")
        if self.spawn_error is not None:
            raise self.spawn_error
        return self

    def status_text(self) -> str:
        self.events.append("status")
        self.clock_now += self.inspection_takes
        if isinstance(self.status, OSError):
            raise self.status
        return self.status

    def release(self) -> None:
        self.events.append("release")
        self.clock_now += self.release_takes
        if self.release_error is not None:
            raise self.release_error

    def kill(self) -> None:
        self.events.append("kill")

    def wait_within(self, seconds: float) -> int | None:
        self.events.append("wait-within")
        self.budgets_waited.append(seconds)
        if len(self.budgets_waited) <= self.headers_stalled:
            return None
        if self.answer:
            os.write(self.spawned[-1].pass_fds[0], self.answer)
        return self.exit_status

    def wait(self) -> int:
        self.events.append("wait")
        return self.exit_status

    def clock(self) -> float:
        return self.clock_now

    def close_gate(self) -> None:
        self.events.append("close-gate")

    def sweep(self, deadline: float) -> bool:
        self.events.append(f"sweep {deadline}")
        if self.sweep_error is not None:
            raise self.sweep_error
        served_enough = (
            self.connections_served_before_sweep_fails is not None
            and len(self.spawned) >= self.connections_served_before_sweep_fails
        )
        return self.sweep_quiet and not served_enough

    def arm(self, remaining: float) -> None:
        self.events.append(f"arm {remaining}")

    def cancel(self) -> None:
        self.events.append("cancel")

    def serving(self) -> Serving:
        return Serving(
            spawn=self.spawn,
            gate_admits=lambda _status: self.gate_admits,
            sweep=self.sweep,
            watchdog=self,
            clock=self.clock,
        )


@contextmanager
def serving(
    volume: Path, runtime_uid: int, admitted: int
) -> Generator[tuple[threading.Thread, list[Spawned], list[object]]]:
    life = ScriptedLife(connections_served_before_sweep_fails=admitted)
    handled = life.spawned
    outcome: list[object] = []

    def run() -> None:
        outcome.append(
            worker.serve(
                volume,
                controller_uid=OWN_UID,
                controller_gid=OWN_GID,
                runtime_uid=runtime_uid,
                serving=life.serving(),
            )
        )

    thread = ServingThread(target=run, daemon=True)

    def finish() -> None:
        life.spawn_error = OSError("cancelled")
        wake_until_ended(volume, thread)
        assert not thread.is_alive()

    try:
        thread.start()
        yield thread, handled, outcome
    except BaseException as body_error:
        finish()
        if thread.failure is not None:
            body_error.add_note(f"the serving thread also failed: {thread.failure!r}")
        raise
    finish()
    if thread.failure is not None:
        raise thread.failure


class ServingThread(threading.Thread):
    """Keeps the error that ended the thread while pytest still sees it."""

    failure: BaseException | None = None

    def run(self) -> None:
        try:
            super().run()
        except BaseException as error:
            self.failure = error
            raise


def wake_until_ended(volume: Path, thread: threading.Thread) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while thread.is_alive() and (time_left := deadline - time.monotonic()) > 0:
        with suppress(OSError):
            connect(volume, timeout=time_left).close()
        if (time_left := deadline - time.monotonic()) > 0:
            thread.join(min(0.05, time_left))


def connect(volume: Path, timeout: float = WAIT_SECONDS) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(volume / worker.SOCKET_NAME))
    return client


def connect_when_bound(volume: Path, thread: threading.Thread) -> socket.socket:
    deadline = time.monotonic() + WAIT_SECONDS
    while thread.is_alive() and time.monotonic() < deadline:
        try:
            return connect(volume)
        except (FileNotFoundError, ConnectionRefusedError):
            continue
    pytest.fail("the worker did not start serving")


class BoundedListener(socket.socket):
    """A listener that stops the accept loop after a fixed number of accepts."""

    accepts_left = 0

    def accept(self) -> tuple[socket.socket, object]:
        if self.accepts_left == 0:
            raise StopServingError
        self.accepts_left -= 1
        return super().accept()


def listening_with(
    volume: Path, accepts: int, listener_class: type[BoundedListener] = BoundedListener
) -> BoundedListener:
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    plain = worker.bind_socket(volume, OWN_UID, OWN_GID)
    listener = listener_class(fileno=plain.detach())
    listener.accepts_left = accepts
    worker.start_listening(listener)
    return listener
