"""Shared arrangement for the build worker's socket and serving-loop tests."""

import errno
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from presentator.adapters import worker_children
from presentator.adapters.build_wire import (
    END_COUNTS,
    LENGTH,
    MAXIMUM_ENTRIES,
    SIZE,
    RecordTag,
    read_request,
)
from presentator.host import worker
from presentator.host.worker import create_request_record
from presentator.host.worker_serving import Serving, StepEffects, WireChild
from presentator.host.worker_wire import WireExit, WireRole
from tests.step_support import (
    EXIT_DESCRIPTOR,
    GATED_STATUS,
    STEP_ENVIRONMENT,
    STEP_EXIT_DESCRIPTOR,
    STEP_PROJECT,
    UNUSED_STEP,
    ScriptedStep,
)

OWN_UID = os.getuid()


UNUSED_WORK = Path("/unused/work")


OWN_GID = os.getgid()


FOREIGN_UID = OWN_UID + 1


PRIVATE_MODE = 0o700


DECK_OPEN_MODE = 0o1777


DECK_CLOSED_MODE = 0o755


CONNECTIONS = 2


WAIT_SECONDS = 5


BULK_SEND_SECONDS = 60
STEP = b"PBW\x01\x02"


def framed(header: bytes, preamble: bytes = b"PBW\x01\x01") -> bytes:
    """A wire message: the preamble, the header length and the header."""
    return preamble + len(header).to_bytes(2, "big") + header


HELLO = b"PBW\x01\x01\x00\x02{}"
STATUS = b"PBW\x01\x03\x00\x02{}"
OUTCOME_FAILED = b'PBW\x01\x05\x00\x20{"outcome":"failed","text":null}'
OUTCOME_BUILT = framed(b'{"outcome":"built"}', b"PBW\x01\x05")
OUTCOME_REFUSED_SHAPE = framed(
    b'{"outcome":"refused","limit":"shape","written":null}', b"PBW\x01\x05"
)
END = b"\x03" + bytes(12)
STEP_HEADER = b'{"step":"build","slug":"my-deck"}'
VALID_STEP = framed(STEP_HEADER, STEP)


def directory_entry(path: bytes) -> bytes:
    """A DIR record: the tag, the path length and the raw path."""
    return bytes([RecordTag.DIR]) + LENGTH.pack(len(path)) + path


def file_entry(path: bytes, content: bytes) -> bytes:
    """A FILE record: the tag, the path, the content length and the content."""
    return (
        bytes([RecordTag.FILE])
        + LENGTH.pack(len(path))
        + path
        + SIZE.pack(len(content))
        + content
    )


def file_header(path: bytes, size: int) -> bytes:
    """A FILE record up to its declared size, with no payload."""
    return file_entry(path, b"")[: -SIZE.size] + SIZE.pack(size)


def talk_entries(index: bytes) -> bytes:
    """The entries of a build whose talk holds only index.html, with their END."""
    return (
        directory_entry(b"talk")
        + file_entry(b"talk/index.html", index)
        + end_entry(2, len(index))
    )


def entries_at_the_maximum() -> bytes:
    """MAXIMUM_ENTRIES entries: a directory and empty files below it."""
    return directory_entry(b"d") + b"".join(
        file_entry(b"d/%05d" % number, b"") for number in range(MAXIMUM_ENTRIES - 1)
    )


def end_entry(count: int, total: int) -> bytes:
    """An END record carrying the claimed entry count and byte sum."""
    return bytes([RecordTag.END]) + END_COUNTS.pack(count, total)


@contextmanager
def listening_socket() -> Generator[socket.socket]:
    """A listening socket on a private abstract address, closed at the end."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listening:
        listening.bind(f"\0{uuid.uuid4().hex}")
        listening.listen(worker.LISTEN_BACKLOG)
        yield listening


def role_of(command: Sequence[str]) -> str:
    """The wire role a spawned child's command line names."""
    return command[command.index("--role") + 1]


class StopServingError(Exception):
    """Ends an accept loop the test bounds."""


ACCEPT_TIME = 100.0


CHILD_LEG = ["spawn", "status", "release", "readable", "wait", "close"]


SENDER_LEG = [
    "spawn",
    "status",
    "connection-check",
    "release",
    "readable",
    "wait",
    "close",
]


IDLE_LEG = ["spawn", "status", "release", "wait-within", "close"]


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


def nothing(_deck: Path) -> None:
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
    wake_takes: float = 0.0
    listener_wakes: int = 0
    scripted_roles: tuple[WireRole, ...] = (WireRole.UPLOAD,)
    send_runs_for: float = 0.0
    exit_with_listener: bool = False
    empty_wakes: int = 0
    kill_error: BaseException | None = None
    reap_error: BaseException | None = None
    headers_stalled: int = 0
    killed: bool = False
    clock_now: float = ACCEPT_TIME
    inspection_takes: float = 0.0
    build_timeout_seconds: float = 300.0
    build_output_megabytes: int = 300
    work: Path = UNUSED_WORK
    connection_ready_at_check: bool = False
    prepared_descriptors: list[int] = field(default_factory=list[int])
    connection_checks: list[tuple[tuple[int, ...], float]] = field(
        default_factory=list[tuple[tuple[int, ...], float]]
    )
    step: ScriptedStep = field(default_factory=ScriptedStep)
    decks_handled: list[Path] = field(default_factory=list[Path])
    release_takes: float = 0.0
    budgets_waited: list[float] = field(default_factory=list[float])
    sweep_error: OSError | None = None
    answer: bytes = b""
    sweep_quiet: bool = True
    quiesce_quiet: bool = True
    quiesce_overrun: float = 0.0
    deck_close_takes: float = 0.0
    connections_served_before_sweep_fails: int | None = None
    spawned: list[Spawned] = field(default_factory=list[Spawned])
    sealed_descriptors: list[int] = field(default_factory=list[int])
    creation: RecordingCreation = field(default_factory=RecordingCreation)

    def __post_init__(self) -> None:
        """Hand the scripted step the life whose events it joins."""
        self.step.life = self

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
        return WireRole(role_of(self.spawned[-1].command))

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
        self.killed = True
        if self.kill_error is not None:
            raise self.kill_error

    def wait_within(self, seconds: float) -> int | None:
        self.events.append("wait-within")
        self.budgets_waited.append(seconds)
        self.raise_wait_error_at_this_wait()
        if len(self.budgets_waited) <= self.headers_stalled:
            return None
        if self.answer:
            self.write_answer_after_hello(self.spawned[-1].pass_fds[0])
        return self.exit_status

    @property
    def exit_descriptor(self) -> int:
        return EXIT_DESCRIPTOR

    def readable(self, descriptors: Sequence[int], seconds: float) -> frozenset[int]:
        if STEP_EXIT_DESCRIPTOR in descriptors:
            return self.step.woken(descriptors, seconds)
        if seconds == 0.0 and len(descriptors) == 1:
            self.events.append("connection-check")
            self.connection_checks.append((tuple(descriptors), seconds))
            ready = self.connection_ready_at_check
            return frozenset(descriptors) if ready else frozenset()
        self.events.append("readable")
        self.budgets_waited.append(seconds)
        self.raise_wait_error_at_this_wait()
        self.clock_now += self.wake_takes
        listener = frozenset(descriptors) - {EXIT_DESCRIPTOR}
        scripted = self.role in self.scripted_roles
        if self.listener_wakes > 0 and listener and scripted:
            self.listener_wakes -= 1
            exit_too = (
                frozenset({EXIT_DESCRIPTOR})
                if self.exit_with_listener
                else frozenset[int]()
            )
            return listener | exit_too
        if self.empty_wakes > 0 and scripted:
            self.empty_wakes -= 1
            return frozenset()
        upload = self.role == WireRole.UPLOAD
        self.clock_now += self.upload_runs_for if upload else self.send_runs_for
        if self.upload_stalled if upload else self.send_stalled:
            self.clock_now += seconds
            return frozenset()
        return frozenset({EXIT_DESCRIPTOR})

    def raise_wait_error_at_this_wait(self) -> None:
        if (
            self.wait_error is not None
            and len(self.budgets_waited) == self.wait_error_at + 1
        ):
            raise self.wait_error

    def write_answer_after_hello(self, connection_fd: int) -> None:
        with socket.socket(fileno=os.dup(connection_fd)) as connection:
            connection.settimeout(WAIT_SECONDS)
            read_request(connection)
        os.write(connection_fd, self.answer)

    def wait(self) -> int:
        self.events.append("wait")
        if self.killed and self.reap_error is not None:
            raise self.reap_error
        return {
            WireRole.IDLE: self.exit_status,
            WireRole.UPLOAD: self.upload_exit_status,
            WireRole.SEND: self.send_exit_status,
        }[self.role]

    def clock(self) -> float:
        return self.clock_now

    def close(self) -> None:
        self.events.append("close")

    def sweep(self, deadline: float) -> bool:
        self.events.append(f"sweep {deadline}")
        if self.sweep_error is not None:
            raise self.sweep_error
        if "open-deck" in self.events:
            return self.quiesced(deadline)
        served_enough = (
            self.connections_served_before_sweep_fails is not None
            and len(self.spawned) >= self.connections_served_before_sweep_fails
        )
        return self.sweep_quiet and not served_enough

    def quiesced(self, deadline: float) -> bool:
        """The answer of the sweep after the step; an overrun is never quiet."""
        if self.quiesce_overrun > 0:
            self.clock_now = max(self.clock_now, deadline + self.quiesce_overrun)
            return False
        return self.quiesce_quiet

    def arm(self, remaining: float) -> None:
        self.events.append(f"arm {remaining}")
        self.clock_now += self.arm_takes

    def seal(self, descriptor: int) -> None:
        self.events.append("seal")
        self.sealed_descriptors.append(descriptor)
        self.clock_now += self.seal_takes
        if self.seal_error is not None:
            raise self.seal_error

    def open_deck(self, deck: Path) -> None:
        self.events.append("open-deck")
        self.decks_handled.append(deck)
        if self.deck_open_error is not None:
            raise self.deck_open_error

    def close_deck(self, deck: Path) -> None:
        self.events.append("close-deck")
        self.decks_handled.append(deck)
        self.clock_now += self.deck_close_takes
        if self.deck_close_error is not None:
            raise self.deck_close_error

    def cancel(self) -> None:
        self.events.append("cancel")

    def prepare(self, _work: Path) -> int:
        self.events.append("prepare")
        self.clock_now += self.step.prepare_takes
        if self.step.prepare_error is not None:
            raise self.step.prepare_error
        descriptor = os.open(os.devnull, os.O_WRONLY)
        self.prepared_descriptors.append(descriptor)
        return descriptor

    def serving(self) -> Serving:
        return Serving(
            spawn=self.spawn,
            gate_admits=self.admits,
            sweep=self.sweep,
            create_record=self.creation,
            seal=self.seal,
            open_deck=self.open_deck,
            close_deck=self.close_deck,
            work=self.work,
            step=StepEffects(
                spawn=self.step.spawn,
                gate_admits=self.step.admits,
                prepare=self.prepare,
                environment=STEP_ENVIRONMENT,
                project=STEP_PROJECT,
            ),
            watchdog=self,
            clock=self.clock,
            readable=self.readable,
            build_timeout_seconds=self.build_timeout_seconds,
            build_output_megabytes=self.build_output_megabytes,
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


def connect_when_bound(
    volume: Path,
    still_serving: Callable[[], bool],
    bind_seconds: float = WAIT_SECONDS,
    timeout: float = WAIT_SECONDS,
) -> socket.socket:
    deadline = time.monotonic() + bind_seconds
    while still_serving() and time.monotonic() < deadline:
        try:
            return connect(volume, timeout)
        except (FileNotFoundError, ConnectionRefusedError):
            continue
    pytest.fail("the worker did not start serving")


def connect_when_serving(
    volume: Path, controller: worker_children.GatedChild, bind_seconds: float
) -> socket.socket:
    return connect_when_bound(
        volume, lambda: controller.wait_within(0.05) is None, bind_seconds
    )


def process_id_of(child: worker_children.GatedChild) -> int:
    return int(child.status_text().split("Pid:")[1].split()[0])


def sender_leg(life: ScriptedLife) -> list[str]:
    """The events from the sender's own spawn, the last of the life."""
    start = len(life.events) - 1 - life.events[::-1].index("spawn")
    return life.events[start:]


def step_leg(life: ScriptedLife) -> list[str]:
    """The events from the step's paths to the quiesce sweep, which it excludes."""
    sweep = next(
        position for position, event in enumerate(life.events) if "sweep" in event
    )
    return life.events[life.events.index("prepare") : sweep]


def present_among(process_ids: Sequence[int]) -> frozenset[int]:
    """The process ids that still exist, a killed child nobody reaped included."""
    present: set[int] = set()
    for process_id in process_ids:
        with suppress(ProcessLookupError):
            os.kill(process_id, 0)
            present.add(process_id)
    return frozenset(present)


def assert_gone(process_ids: Sequence[int]) -> None:
    assert not present_among(process_ids)


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


def serving_with(
    spawn: Callable[..., WireChild], *, admits: bool = True, quiet: bool = True
) -> Serving:
    return Serving(
        spawn=spawn,
        gate_admits=lambda _status: admits,
        sweep=lambda _deadline: quiet,
        create_record=worker.create_request_record,
        seal=worker.seal_record,
        open_deck=nothing,
        close_deck=nothing,
        work=UNUSED_WORK,
        step=UNUSED_STEP,
        watchdog=NeverArmed(),
        clock=time.monotonic,
        readable=worker_children.readable_within,
        build_timeout_seconds=300.0,
        build_output_megabytes=300,
    )


def accept_time() -> float:
    return time.monotonic()


def open_descriptor_count() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))
