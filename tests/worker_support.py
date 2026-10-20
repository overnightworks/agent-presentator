"""Shared arrangement for the build worker's socket and serving-loop tests."""

import errno
import os
import socket
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from presentator.adapters.build_wire import read_request
from presentator.host import worker
from presentator.host.worker import create_request_record
from presentator.host.worker_serving import Serving
from presentator.host.worker_wire import WireExit, WireRole

OWN_UID = os.getuid()


OWN_GID = os.getgid()


FOREIGN_UID = OWN_UID + 1


PRIVATE_MODE = 0o700


DECK_OPEN_MODE = 0o1777


DECK_CLOSED_MODE = 0o755


CONNECTIONS = 2


WAIT_SECONDS = 5


class StopServingError(Exception):
    """Ends an accept loop the test bounds."""


GATED_STATUS = "Uid:\t10002\t10002\t10002\t10002"


ACCEPT_TIME = 100.0


ADMITTING_PATH = "/opt/test/bin"


CHILD_LEG = ["spawn", "status", "release", "wait-within", "close-gate"]


@dataclass(frozen=True)
class Spawned:
    """What the controller asked a spawn for."""

    command: Sequence[str]
    environment: Mapping[str, str]
    pass_fds: Sequence[int]


class RecordingCreation:
    """Remembers the record the controller created and which object it is."""

    descriptor: int
    identity: tuple[int, int]

    def __call__(self) -> int:
        self.descriptor = create_request_record()
        made = os.fstat(self.descriptor)
        self.identity = (made.st_dev, made.st_ino)
        return self.descriptor

    def is_closed(self) -> bool:
        try:
            held = os.fstat(self.descriptor)
        except OSError as error:
            return error.errno == errno.EBADF
        return (held.st_dev, held.st_ino) != self.identity


class NeverArmed:
    """A watchdog that must not arm a real timer inside pytest."""

    def arm(self, remaining: float) -> None:
        pass

    def cancel(self) -> None:
        pass


def nothing() -> None:
    """A deck effect for tests that never reach /work."""


@dataclass
class ScriptedLife:
    """One serving life's effects and its wire child, recorded in order."""

    events: list[str] = field(default_factory=list[str])
    spawn_error: OSError | None = None
    status: str | OSError = GATED_STATUS
    gate_admits: bool = True
    release_error: OSError | None = None
    exit_status: int = WireExit.CLOSED
    upload_exit_status: int = WireExit.UPLOADED
    upload_stalled: bool = False
    upload_spawn_error: OSError | None = None
    upload_gate_admits: bool = True
    send_exit_status: int = WireExit.SENT
    send_stalled: bool = False
    send_spawn_error: OSError | None = None
    send_gate_admits: bool = True
    send_status: str | OSError = GATED_STATUS
    send_release_error: OSError | None = None
    seal_error: OSError | None = None
    deck_open_error: OSError | None = None
    deck_close_error: OSError | None = None
    arm_takes: float = 0.0
    seal_takes: float = 0.0
    upload_spawn_takes: float = 0.0
    send_spawn_takes: float = 0.0
    upload_runs_for: float = 0.0
    wait_error: OSError | None = None
    wait_error_at: int = 0
    kill_error: BaseException | None = None
    reap_error: BaseException | None = None
    headers_stalled: int = 0
    clock_now: float = ACCEPT_TIME
    inspection_takes: float = 0.0
    build_timeout_seconds: float = 300.0
    release_takes: float = 0.0
    budgets_waited: list[float] = field(default_factory=list[float])
    sweep_error: OSError | None = None
    answer: bytes = b""
    sweep_quiet: bool = True
    connections_served_before_sweep_fails: int | None = None
    spawned: list[Spawned] = field(default_factory=list[Spawned])
    sealed_descriptors: list[int] = field(default_factory=list[int])
    creation: RecordingCreation = field(default_factory=RecordingCreation)

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
        if self.role == WireRole.UPLOAD:
            self.clock_now += self.upload_spawn_takes
            if self.upload_spawn_error is not None:
                raise self.upload_spawn_error
        if self.role == WireRole.SEND:
            self.clock_now += self.send_spawn_takes
            if self.send_spawn_error is not None:
                raise self.send_spawn_error
        return self

    @property
    def role(self) -> WireRole:
        command = self.spawned[-1].command
        return WireRole(command[command.index("--role") + 1])

    def admits(self, _status: str) -> bool:
        return {
            WireRole.IDLE: self.gate_admits,
            WireRole.UPLOAD: self.upload_gate_admits,
            WireRole.SEND: self.send_gate_admits,
        }[self.role]

    def status_text(self) -> str:
        self.events.append("status")
        self.clock_now += self.inspection_takes
        status = self.send_status if self.role == WireRole.SEND else self.status
        if isinstance(status, OSError):
            raise status
        return status

    def release(self) -> None:
        self.events.append("release")
        self.clock_now += self.release_takes
        error = (
            self.send_release_error
            if self.role == WireRole.SEND
            else self.release_error
        )
        if error is not None:
            raise error

    def kill(self) -> None:
        self.events.append("kill")
        if self.kill_error is not None:
            raise self.kill_error

    def wait_within(self, seconds: float) -> int | None:
        self.events.append("wait-within")
        self.budgets_waited.append(seconds)
        if (
            self.wait_error is not None
            and len(self.budgets_waited) == self.wait_error_at + 1
        ):
            raise self.wait_error
        if self.role == WireRole.UPLOAD:
            self.clock_now += self.upload_runs_for
            return None if self.upload_stalled else self.upload_exit_status
        if self.role == WireRole.SEND:
            return None if self.send_stalled else self.send_exit_status
        if len(self.budgets_waited) <= self.headers_stalled:
            return None
        if self.answer:
            self.write_answer_after_hello(self.spawned[-1].pass_fds[0])
        return self.exit_status

    def write_answer_after_hello(self, connection_fd: int) -> None:
        with socket.socket(fileno=os.dup(connection_fd)) as connection:
            connection.settimeout(WAIT_SECONDS)
            read_request(connection)
        os.write(connection_fd, self.answer)

    def wait(self) -> int:
        self.events.append("wait")
        if self.reap_error is not None:
            raise self.reap_error
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
        self.clock_now += self.arm_takes

    def seal(self, descriptor: int) -> None:
        self.events.append("seal")
        self.sealed_descriptors.append(descriptor)
        self.clock_now += self.seal_takes
        if self.seal_error is not None:
            raise self.seal_error

    def open_deck(self) -> None:
        self.events.append("open-deck")
        if self.deck_open_error is not None:
            raise self.deck_open_error

    def close_deck(self) -> None:
        self.events.append("close-deck")
        if self.deck_close_error is not None:
            raise self.deck_close_error

    def cancel(self) -> None:
        self.events.append("cancel")

    def serving(self) -> Serving:
        return Serving(
            spawn=self.spawn,
            gate_admits=self.admits,
            sweep=self.sweep,
            create_record=self.creation,
            seal=self.seal,
            open_deck=self.open_deck,
            close_deck=self.close_deck,
            watchdog=self,
            clock=self.clock,
            build_timeout_seconds=self.build_timeout_seconds,
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
