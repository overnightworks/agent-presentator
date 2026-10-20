"""The gated spawn and the wire entry, run for real as the test user."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import ClassVar

import pytest

from presentator.adapters import worker_children
from presentator.adapters.worker_children import ChildIdentity, GatedChild, spawn_gated
from presentator.host import worker
from presentator.host.worker import LifeEnd, Serving, WireChild

OWN_UID = os.getuid()
OWN_GID = os.getgid()
OWN_IDENTITY = ChildIdentity(OWN_UID, OWN_GID, extra_groups=None)
WAIT_SECONDS = 10
WATCHDOG_WINDOW = (6.0, 8.0)
INHERITED_DESCRIPTORS = {0, 1, 2}
RELEASED_UMASK = 0o022
CURRENT_DIRECTORY_KEY = "PWD"
PATH = "/usr/bin:/bin"
SECOND_CONNECTION = 2
STALLED_CONTROLLER = """
import ctypes, os, sys, time
from pathlib import Path
from presentator.host import worker
from presentator.host.worker_termination import FaultHandlerWatchdog

def stall(*_arguments, **_keywords):
    ctypes.PyDLL(None).pause()

def clock_announcing_each_reading():
    reading = time.monotonic()
    print(reading, flush=True)
    return reading

serving = worker.Serving(
    spawn=stall,
    gate_admits=lambda _status: True,
    sweep=lambda _deadline: True,
    watchdog=FaultHandlerWatchdog(),
    clock=clock_announcing_each_reading,
)
sys.exit(
    worker.serve(
        Path(sys.argv[1]),
        controller_uid=os.getuid(),
        controller_gid=os.getgid(),
        runtime_uid=os.getuid(),
        serving=serving,
    )
)
"""
PROBE = """
import json, os, sys
descriptor = int(sys.argv[1])
open_descriptors = sorted(
    int(name)
    for name in os.listdir("/proc/self/fd")
    if os.path.exists("/proc/self/fd/" + name)
)
report = {
    "stdin": os.readlink("/proc/self/fd/0"),
    "descriptors": open_descriptors,
    "connection": descriptor,
    "working_directory": os.getcwd(),
    "umask": os.umask(0),
    "session_leader": os.getsid(0) == os.getpid(),
    "environment": dict(os.environ),
    "oom_score_adj": open("/proc/self/oom_score_adj").read().strip(),
}
os.write(descriptor, json.dumps(report).encode())
sys.exit(3)
"""
MARKER_WRITER = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')"


def marker_writer(marker: Path) -> GatedChild:
    return spawn_gated(
        [sys.executable, "-I", "-c", MARKER_WRITER, str(marker)],
        identity=OWN_IDENTITY,
        environment={"PATH": PATH},
        pass_fds=(),
    )


def run_program(source: str, command: Sequence[str]) -> list[str]:
    connection_flag = command.index("--connection-fd")
    return [sys.executable, "-I", "-c", source, command[connection_flag + 1]]


def spawn_probe(source: str) -> Callable[..., WireChild]:
    def spawn(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> GatedChild:
        return spawn_gated(
            run_program(source, command),
            identity=OWN_IDENTITY,
            environment=environment,
            pass_fds=pass_fds,
        )

    return spawn


def serving_with(
    spawn: Callable[..., WireChild], *, admits: bool = True, quiet: bool = True
) -> Serving:
    return Serving(
        spawn=spawn,
        gate_admits=lambda _status: admits,
        sweep=lambda _deadline: quiet,
        watchdog=NeverArmed(),
        clock=time.monotonic,
    )


class NeverArmed:
    """A watchdog that must not arm a real timer inside pytest."""

    def arm(self, remaining: float) -> None:
        pass

    def cancel(self) -> None:
        pass


def open_descriptor_count() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


def read_to_end(client: socket.socket) -> bytes:
    received = b""
    while chunk := client.recv(4096):
        received += chunk
    return received


@pytest.fixture
def connection_pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    controller_end, client = socket.socketpair()
    client.settimeout(WAIT_SECONDS)
    with closing(controller_end), closing(client):
        yield controller_end, client


@dataclass(frozen=True)
class ProbeReport:
    """What the released probe saw of itself."""

    stdin: str
    descriptors: list[int]
    connection: int
    working_directory: str
    umask: int
    session_leader: bool
    environment: dict[str, str]
    oom_score_adj: str


def hand_over_probe(
    connection_pair: tuple[socket.socket, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> ProbeReport:
    monkeypatch.setenv("PATH", PATH)
    connection, client = connection_pair
    kept = worker.hand_over(connection, serving_with(spawn_probe(PROBE)))
    connection.close()
    assert kept is None
    return ProbeReport(**json.loads(read_to_end(client)))


def test_a_released_child_runs_with_only_its_connection_and_a_clean_environment(
    connection_pair: tuple[socket.socket, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = hand_over_probe(connection_pair, monkeypatch)

    assert report.stdin == "/dev/null"
    assert set(report.descriptors) == INHERITED_DESCRIPTORS | {report.connection}
    assert report.working_directory == "/"
    assert report.umask == RELEASED_UMASK
    assert report.session_leader
    assert report.oom_score_adj == "1000"


def test_a_released_child_holds_nothing_of_the_controllers_environment(
    connection_pair: tuple[socket.socket, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_FOR_THE_CONTROLLER", "leak")

    environment = hand_over_probe(connection_pair, monkeypatch).environment

    assert set(environment) <= {"PATH", CURRENT_DIRECTORY_KEY, "LC_CTYPE"}
    assert environment["PATH"] == PATH
    assert environment[CURRENT_DIRECTORY_KEY] == "/"


def test_a_refused_gate_kills_the_child_before_it_runs_and_closes_without_a_reply(
    connection_pair: tuple[socket.socket, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", PATH)
    connection, client = connection_pair

    refused = worker.hand_over(
        connection, serving_with(spawn_probe(PROBE), admits=False)
    )
    connection.close()

    assert refused == LifeEnd.GATE
    assert read_to_end(client) == b""


@pytest.mark.parametrize(
    "release_text",
    [None, b"go"],
    ids=["a bare end of file", "go before the end of file"],
)
def test_a_gate_that_is_not_released_with_a_line_never_runs_the_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_text: bytes | None,
) -> None:
    marker = tmp_path / "marker"
    child = marker_writer(marker)

    if release_text is not None:
        monkeypatch.setattr(worker_children, "RELEASE", release_text)
        child.release()
    child.close_gate()

    assert child.wait() != 0
    assert not marker.exists()


def test_a_line_releases_the_program(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    child = marker_writer(marker)

    child.release()
    child.close_gate()

    assert child.wait() == 0
    assert marker.read_text() == "ran"


def test_a_dead_child_breaks_the_release_pipe_and_has_no_readable_status() -> None:
    child = spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )
    child.kill()
    child.wait()

    with pytest.raises(BrokenPipeError):
        child.release()
    with pytest.raises(FileNotFoundError):
        child.status_text()
    child.close_gate()


def test_the_status_of_a_waiting_child_names_the_user_it_runs_as() -> None:
    child = spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )
    try:
        status = child.status_text()
    finally:
        child.kill()
        child.wait()
        child.close_gate()

    assert f"Uid:\t{OWN_UID}\t{OWN_UID}\t{OWN_UID}\t{OWN_UID}" in status


def test_both_gate_ends_are_closed_after_a_child_ran_and_after_a_spawn_failed() -> None:
    before = open_descriptor_count()
    child = spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )
    child.release()
    child.close_gate()
    child.wait()
    foreign = ChildIdentity(OWN_UID + 100_000, OWN_GID, extra_groups=None)
    with pytest.raises(PermissionError):
        spawn_gated(
            ["/bin/true"], identity=foreign, environment={"PATH": PATH}, pass_fds=()
        )

    assert open_descriptor_count() == before


class RecordedPopen:
    """Records how the controller spawns and then fails the spawn."""

    FAILURE: ClassVar[str] = "popen recorded"
    calls: ClassVar[list[tuple[object, dict[str, object]]]] = []

    def __init__(self, arguments: object, **keywords: object) -> None:
        """Record the call and fail."""
        self.calls.append((arguments, keywords))
        raise OSError(self.FAILURE)


def test_production_spawns_the_wire_child_as_the_wire_user_without_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RecordedPopen, "calls", [])
    monkeypatch.setattr(worker_children.subprocess, "Popen", RecordedPopen)

    with pytest.raises(OSError, match=RecordedPopen.FAILURE):
        worker.REAL_SERVING.spawn(["wire"], environment={"PATH": PATH}, pass_fds=(7,))

    [(_arguments, keywords)] = RecordedPopen.calls
    wire_id = worker.WIRE_IDENTITY[0]
    assert (keywords["user"], keywords["group"], keywords["extra_groups"]) == (
        wire_id,
        wire_id,
        [],
    )
    assert keywords["env"] == {"PATH": PATH}
    assert keywords["pass_fds"] == (7,)
    assert keywords["cwd"] == "/"
    assert keywords["start_new_session"] is True


@pytest.fixture
def socket_directory(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    before = os.umask(0)
    os.umask(before)
    directory = tmp_path_factory.mktemp("w")
    directory.chmod(0o700)
    yield directory
    os.umask(before)


@dataclass
class RecordingWatchdog:
    """Records arm and cancel without ever arming a real timer."""

    events: list[str] = field(default_factory=list[str])

    def arm(self, remaining: float) -> None:
        self.events.append("arm")

    def cancel(self) -> None:
        self.events.append("cancel")


@dataclass(frozen=True)
class Reply:
    """What a client read, and whether the stream ended by reset rather than EOF."""

    received: bytes
    ended_by_reset: bool


@dataclass
class RealServing:
    """A serve loop over the real gated spawn, ending after some connections."""

    directory: Path
    connections: int
    sweep_deadlines: list[float] = field(default_factory=list[float])
    watchdog: RecordingWatchdog = field(default_factory=RecordingWatchdog)
    outcome: list[int] = field(default_factory=list[int])
    swept: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))

    def sweep(self, deadline: float) -> bool:
        self.sweep_deadlines.append(deadline)
        self.swept.release()
        return len(self.sweep_deadlines) < self.connections

    def await_sweep(self) -> None:
        assert self.swept.acquire(timeout=WAIT_SECONDS)

    def start(self) -> threading.Thread:
        serving = Serving(
            spawn=partial(spawn_gated, identity=OWN_IDENTITY),
            gate_admits=lambda _status: True,
            sweep=self.sweep,
            watchdog=self.watchdog,
            clock=time.monotonic,
        )
        thread = threading.Thread(
            target=lambda: self.outcome.append(
                worker.serve(
                    self.directory,
                    controller_uid=OWN_UID,
                    controller_gid=OWN_GID,
                    runtime_uid=OWN_UID,
                    serving=serving,
                )
            ),
            daemon=True,
        )
        thread.start()
        return thread

    def exchange(self, thread: threading.Thread, sent: bytes) -> Reply:
        with closing(connect_when_bound(self.directory, thread.is_alive)) as client:
            client.sendall(sent)
            received = b""
            try:
                while chunk := client.recv(64):
                    received += chunk
            except ConnectionResetError:
                return Reply(received, ended_by_reset=True)
            return Reply(received, ended_by_reset=False)


HELLO = b"PBW\x01\x01\x00\x02{}"
STATUS = b"PBW\x01\x03\x00\x02{}"
STEP_PREAMBLE_AND_HEADER = b"PBW\x01\x02\x00\x02{}"
HEADER_DEADLINE_SECONDS = 2.0


def test_the_real_entry_answers_a_hello_twice_in_one_process_within_the_header_deadline(
    socket_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", PATH)
    serving = RealServing(socket_directory, connections=SECOND_CONNECTION)
    thread = serving.start()

    for _ in range(SECOND_CONNECTION):
        before = time.monotonic()
        reply = serving.exchange(thread, HELLO)
        after = time.monotonic()
        serving.await_sweep()

        assert reply == Reply(STATUS, ended_by_reset=False)
        assert after - before < HEADER_DEADLINE_SECONDS
        assert before + worker.SWEEP_SECONDS <= serving.sweep_deadlines[-1]
        assert serving.sweep_deadlines[-1] <= after + worker.SWEEP_SECONDS
    thread.join(WAIT_SECONDS)

    assert serving.outcome == [0]
    assert serving.watchdog.events == ["arm", "cancel", "arm"]


def test_a_step_is_closed_unserved_and_the_same_process_then_answers_a_hello(
    socket_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", PATH)
    serving = RealServing(socket_directory, connections=SECOND_CONNECTION)
    thread = serving.start()

    assert serving.exchange(thread, STEP_PREAMBLE_AND_HEADER).received == b""
    assert serving.exchange(thread, HELLO) == Reply(STATUS, ended_by_reset=False)
    thread.join(WAIT_SECONDS)

    assert serving.outcome == [0]
    assert len(serving.sweep_deadlines) == SECOND_CONNECTION


def connect_when_bound(
    directory: Path, still_serving: Callable[[], bool]
) -> socket.socket:
    deadline = time.monotonic() + WAIT_SECONDS
    while still_serving() and time.monotonic() < deadline:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(WAIT_SECONDS)
        try:
            client.connect(str(directory / worker.SOCKET_NAME))
        except (FileNotFoundError, ConnectionRefusedError):
            client.close()
            continue
        return client
    pytest.fail("the worker did not start serving")


def test_a_stalled_controller_serving_an_idle_connection_exits_one_at_seven_seconds(
    socket_directory: Path,
) -> None:
    controller = subprocess.Popen(
        [sys.executable, "-I", "-c", STALLED_CONTROLLER, str(socket_directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        client = connect_when_bound(socket_directory, lambda: controller.poll() is None)
        with closing(client):
            exit_status = controller.wait(timeout=WAIT_SECONDS + 5)
        exited_at = time.monotonic()
        assert controller.stdout is not None
        accepted_at = float(controller.stdout.readline())
    finally:
        controller.kill()
        controller.wait()
        assert controller.stderr is not None
        controller.stderr.close()
        assert controller.stdout is not None
        controller.stdout.close()
    elapsed = exited_at - accepted_at

    assert exit_status == 1
    assert WATCHDOG_WINDOW[0] <= elapsed <= WATCHDOG_WINDOW[1]
