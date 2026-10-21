"""The gated spawn and the wire entry, run for real as the test user."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import ClassVar

import pytest

from presentator.adapters import worker_children
from presentator.adapters.worker_children import ChildIdentity, GatedChild, spawn_gated
from presentator.host import worker, worker_serving
from presentator.host.config import WorkerSettings
from presentator.host.worker_serving import (
    HEADER_SECONDS,
    LifeEnd,
    Serving,
    WireChild,
)
from presentator.host.worker_termination import SWEEP_SECONDS
from tests.worker_support import (
    END,
    HELLO,
    OUTCOME_FAILED,
    STATUS,
    STEP_PREAMBLE,
    VALID_STEP,
    accept_time,
    assert_gone,
    connect_when_bound,
    framed,
    open_descriptor_count,
    process_id_of,
    role_of,
    serving_with,
    wake_until_ended,
)

OWN_UID = os.getuid()
OWN_GID = os.getgid()
OWN_IDENTITY = ChildIdentity(OWN_UID, OWN_GID, extra_groups=None)
REAL_SPAWN = partial(spawn_gated, identity=OWN_IDENTITY)
WAIT_SECONDS = 10
WATCHDOG_WINDOW = (6.0, 8.0)
INHERITED_DESCRIPTORS = {0, 1, 2}
RELEASED_UMASK = 0o022
CURRENT_DIRECTORY_KEY = "PWD"
PATH = "/usr/bin:/bin"
SECOND_CONNECTION = 2
SHORT_BUDGET_SECONDS = 0.05
STALLED_CONTROLLER = """
import ctypes, os, sys, time
from pathlib import Path
from presentator.adapters.worker_children import readable_within
from presentator.host import worker, worker_serving
from presentator.host.worker_termination import FaultHandlerWatchdog

def stall(*_arguments, **_keywords):
    ctypes.PyDLL(None).pause()

def clock_announcing_each_reading():
    reading = time.monotonic()
    print(reading, flush=True)
    return reading

serving = worker_serving.Serving(
    spawn=stall,
    gate_admits=lambda _status: True,
    sweep=lambda _deadline: True,
    create_record=worker.create_request_record,
    seal=lambda _record: None,
    open_deck=lambda _deck: None,
    close_deck=lambda _deck: None,
    deck=Path("/unused/deck"),
    watchdog=FaultHandlerWatchdog(),
    clock=clock_announcing_each_reading,
    readable=readable_within,
    build_timeout_seconds=300.0,
    build_output_megabytes=300,
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
record = int(sys.argv[2])
open_descriptors = sorted(
    int(name)
    for name in os.listdir("/proc/self/fd")
    if os.path.exists("/proc/self/fd/" + name)
)
report = {
    "stdin": os.readlink("/proc/self/fd/0"),
    "descriptors": open_descriptors,
    "connection": descriptor,
    "record": record,
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
    record_flag = command.index("--record-fd")
    return [
        sys.executable,
        "-I",
        "-c",
        source,
        command[connection_flag + 1],
        command[record_flag + 1],
    ]


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


def real_serving() -> Serving:
    return worker.real_serving(WorkerSettings(build_timeout_seconds=300.0))


def read_to_end(client: socket.socket) -> bytes:
    received = b""
    while chunk := client.recv(4096):
        received += chunk
    return received


ConnectionPair = tuple[socket.socket, socket.socket, socket.socket]


@pytest.fixture
def connection_pair(listener: socket.socket) -> Iterator[ConnectionPair]:
    controller_end, client = socket.socketpair()
    client.settimeout(WAIT_SECONDS)
    with closing(controller_end), closing(client):
        yield controller_end, client, listener


@dataclass(frozen=True)
class ProbeReport:
    """What the released probe saw of itself."""

    stdin: str
    descriptors: list[int]
    connection: int
    record: int
    working_directory: str
    umask: int
    session_leader: bool
    environment: dict[str, str]
    oom_score_adj: str


def hand_over_to_probe(
    connection_pair: ConnectionPair,
    monkeypatch: pytest.MonkeyPatch,
    *,
    admits: bool = True,
) -> tuple[LifeEnd | None, bytes]:
    monkeypatch.setenv("PATH", PATH)
    connection, client, listener = connection_pair
    life_end = worker_serving.hand_over(
        connection,
        serving_with(spawn_probe(PROBE), admits=admits),
        accept_time(),
        listener=listener,
    )
    connection.close()
    return life_end, read_to_end(client)


def hand_over_probe(
    connection_pair: ConnectionPair, monkeypatch: pytest.MonkeyPatch
) -> ProbeReport:
    life_end, report = hand_over_to_probe(connection_pair, monkeypatch)
    assert life_end is None
    return ProbeReport(**json.loads(report))


def test_a_released_child_runs_with_only_its_connection_and_a_clean_environment(
    connection_pair: ConnectionPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = hand_over_probe(connection_pair, monkeypatch)

    assert report.stdin == "/dev/null"
    assert set(report.descriptors) == INHERITED_DESCRIPTORS | {
        report.connection,
        report.record,
    }
    assert report.working_directory == "/"
    assert report.umask == RELEASED_UMASK
    assert report.session_leader
    assert report.oom_score_adj == "1000"


def test_a_released_child_holds_nothing_of_the_controllers_environment(
    connection_pair: ConnectionPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_FOR_THE_CONTROLLER", "leak")

    environment = hand_over_probe(connection_pair, monkeypatch).environment

    assert set(environment) <= {"PATH", CURRENT_DIRECTORY_KEY, "LC_CTYPE"}
    assert environment["PATH"] == PATH
    assert environment[CURRENT_DIRECTORY_KEY] == "/"


def test_a_refused_gate_kills_the_child_before_it_runs_and_closes_without_a_reply(
    connection_pair: ConnectionPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    life_end, reply = hand_over_to_probe(connection_pair, monkeypatch, admits=False)

    assert life_end == LifeEnd.GATE
    assert reply == b""


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
    child.close()

    assert child.wait() != 0
    assert not marker.exists()


def test_a_line_releases_the_program(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    child = marker_writer(marker)

    child.release()
    child.close()

    assert child.wait() == 0
    assert marker.read_text() == "ran"


def sleeping_child() -> GatedChild:
    return spawn_gated(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        identity=OWN_IDENTITY,
        environment={"PATH": PATH},
        pass_fds=(),
    )


def release_and_close(child: GatedChild) -> None:
    try:
        child.release()
    finally:
        child.close()


def end_child(child: GatedChild) -> int:
    try:
        child.kill()
    finally:
        reaped_status = child.wait()
    return reaped_status


def test_waiting_within_a_budget_returns_none_and_leaves_the_released_child_alive() -> (
    None
):
    child = sleeping_child()
    try:
        release_and_close(child)
        waited = child.wait_within(SHORT_BUDGET_SECONDS)
    finally:
        reaped_status = end_child(child)

    assert waited is None
    assert reaped_status == -signal.SIGKILL


def test_waiting_within_a_budget_returns_the_status_of_a_child_that_exited() -> None:
    child = spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )
    try:
        release_and_close(child)
        waited = child.wait_within(WAIT_SECONDS)
    finally:
        end_child(child)

    assert waited == 0


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
    child.close()


def test_the_status_of_a_waiting_child_names_the_user_it_runs_as() -> None:
    child = spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )
    try:
        status = child.status_text()
    finally:
        child.kill()
        child.wait()
        child.close()

    assert f"Uid:\t{OWN_UID}\t{OWN_UID}\t{OWN_UID}\t{OWN_UID}" in status


def test_both_gate_ends_are_closed_after_a_child_ran_and_after_a_spawn_failed() -> None:
    before = open_descriptor_count()
    child = spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )
    child.release()
    child.close()
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
    spawn = real_serving().spawn

    with pytest.raises(OSError, match=RecordedPopen.FAILURE):
        spawn(["wire"], environment={"PATH": PATH}, pass_fds=(7,))

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
    spawn: Callable[..., WireChild] = REAL_SPAWN
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
        serving = replace(
            serving_with(self.spawn),
            sweep=self.sweep,
            watchdog=self.watchdog,
            deck=self.directory,
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
        with closing(
            connect_when_bound(self.directory, thread.is_alive, timeout=WAIT_SECONDS)
        ) as client:
            client.sendall(sent)
            received = b""
            try:
                while chunk := client.recv(64):
                    received += chunk
            except ConnectionResetError:
                return Reply(received, ended_by_reset=True)
            return Reply(received, ended_by_reset=False)


REJECTED_STEPS = {
    "a slug leaving its folder": framed(
        b'{"step":"build","slug":"../x"}', STEP_PREAMBLE
    ),
    "an unknown step": framed(b'{"step":"deploy","slug":"x"}', STEP_PREAMBLE),
    "an extra key": framed(b'{"step":"build","slug":"x","more":1}', STEP_PREAMBLE),
}


def test_the_real_entry_answers_a_hello_twice_in_one_process_within_the_header_deadline(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", PATH)
    with dripped_header(volume) as run:
        serving = run.serving
        for _ in range(SECOND_CONNECTION):
            before = time.monotonic()
            reply = serving.exchange(run.thread, HELLO)
            after = time.monotonic()
            serving.await_sweep()

            assert reply.received == STATUS
            assert not reply.ended_by_reset
            assert after - before < HEADER_SECONDS
            assert before + SWEEP_SECONDS <= serving.sweep_deadlines[-1]
            assert serving.sweep_deadlines[-1] <= after + SWEEP_SECONDS
        run.thread.join(WAIT_SECONDS)

    assert serving.outcome == [0]
    assert serving.watchdog.events == ["arm", "cancel", "arm"]


@pytest.mark.parametrize("sent", REJECTED_STEPS.values(), ids=REJECTED_STEPS.keys())
def test_a_rejected_step_is_closed_unserved_and_the_same_process_then_answers_a_hello(
    volume: Path, monkeypatch: pytest.MonkeyPatch, sent: bytes
) -> None:
    monkeypatch.setenv("PATH", PATH)
    with dripped_header(volume) as run:
        serving = run.serving
        assert serving.exchange(run.thread, sent).received == b""
        second_reply = serving.exchange(run.thread, HELLO)
        assert second_reply.received == STATUS
        assert not second_reply.ended_by_reset
        run.thread.join(WAIT_SECONDS)

    assert serving.outcome == [0]
    assert len(serving.sweep_deadlines) == SECOND_CONNECTION
    assert serving.watchdog.events == ["arm", "cancel", "arm"]


def test_a_valid_step_and_end_get_status_failed_end_and_a_clean_eof_and_end_the_life(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", PATH)
    with dripped_header(volume) as run:
        reply = run.serving.exchange(run.thread, VALID_STEP + END)
        run.thread.join(WAIT_SECONDS)

    assert reply.received == STATUS + OUTCOME_FAILED + END
    assert not reply.ended_by_reset
    assert run.spawns.roles == ["idle", "upload", "send"]
    assert not run.thread.is_alive()
    assert run.serving.outcome == [0]
    assert run.serving.sweep_deadlines == []
    assert run.serving.watchdog.events == ["arm", "arm"]


HELLO_PREAMBLE_AND_ONE_HEADER_BYTE = HELLO[:7] + HELLO[7:8]
THREE_PREAMBLE_BYTES = HELLO[:3]
STEP_PREAMBLE_AND_ONE_HEADER_BYTE = VALID_STEP[:7] + VALID_STEP[7:8]


@dataclass
class RecordingSpawn:
    """The real gated spawn that remembers every child and can be made to fail."""

    children: list[GatedChild] = field(default_factory=list[GatedChild])
    roles: list[str] = field(default_factory=list[str])
    process_ids: list[int] = field(default_factory=list[int])
    stopped: bool = False

    def __call__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> GatedChild:
        if self.stopped:
            reason = "the test stopped the serving loop"
            raise OSError(reason)
        child = spawn_gated(
            command, identity=OWN_IDENTITY, environment=environment, pass_fds=pass_fds
        )
        self.children.append(child)
        self.roles.append(role_of(command))
        self.process_ids.append(process_id_of(child))
        return child

    def still_running(self) -> list[GatedChild]:
        return [child for child in self.children if child.wait_within(0.0) is None]

    def kill_and_reap(self) -> None:
        for child in self.children:
            child.kill()
        reaped = [child.wait_within(WAIT_SECONDS) for child in self.children]
        assert None not in reaped, "a killed child was not reaped"
        assert_gone(self.process_ids)


@dataclass
class DrippedHeader:
    """A serving loop over the real wire entry, always stopped and cleaned up."""

    serving: RealServing
    spawns: RecordingSpawn
    thread: threading.Thread
    clients: list[socket.socket] = field(default_factory=list[socket.socket])

    def drip_then_stop_writing(self, dripped: bytes) -> bytes:
        client = connect_when_bound(
            self.serving.directory, self.thread.is_alive, timeout=WAIT_SECONDS
        )
        self.clients.append(client)
        client.sendall(dripped)
        return client.recv(64)

    def stop(self) -> None:
        self.spawns.stopped = True
        wake_until_ended(self.serving.directory, self.thread)
        for client in self.clients:
            client.close()


@contextmanager
def dripped_header(directory: Path) -> Generator[DrippedHeader]:
    spawns = RecordingSpawn()
    serving = RealServing(directory, connections=SECOND_CONNECTION, spawn=spawns)
    run = DrippedHeader(serving, spawns, serving.start())
    try:
        yield run
    finally:
        run.stop()
        survivors = spawns.still_running()
        spawns.kill_and_reap()
        assert not run.thread.is_alive()
        assert survivors == []


@pytest.mark.parametrize(
    "dripped",
    [
        HELLO_PREAMBLE_AND_ONE_HEADER_BYTE,
        STEP_PREAMBLE_AND_ONE_HEADER_BYTE,
        THREE_PREAMBLE_BYTES,
    ],
    ids=[
        "a valid preamble and one header byte",
        "a valid STEP preamble and one header byte",
        "three preamble bytes",
    ],
)
def test_a_header_that_stops_midway_is_cut_off_at_the_deadline_and_serving_goes_on(
    volume: Path, monkeypatch: pytest.MonkeyPatch, dripped: bytes
) -> None:
    monkeypatch.setenv("PATH", PATH)
    with dripped_header(volume) as run:
        received = run.drip_then_stop_writing(dripped)
        ended_at = time.monotonic()
        run.serving.await_sweep()
        [sweep_deadline] = run.serving.sweep_deadlines
        accepted_at = sweep_deadline - SWEEP_SECONDS
        second_reply = run.serving.exchange(run.thread, HELLO)
        run.thread.join(WAIT_SECONDS)

    assert received == b""
    assert accepted_at + HEADER_SECONDS <= ended_at < sweep_deadline
    assert second_reply.received == STATUS
    assert run.serving.watchdog.events == ["arm", "cancel", "arm"]
    assert run.serving.outcome == [0]


class ForcedFailureError(Exception):
    """Fails a test between its two connections."""


def fail_between_the_connections(directory: Path, started: list[DrippedHeader]) -> None:
    with dripped_header(directory) as run:
        started.append(run)
        run.drip_then_stop_writing(THREE_PREAMBLE_BYTES)
        run.serving.await_sweep()
        raise ForcedFailureError


def test_a_failure_between_the_two_connections_leaves_neither_thread_nor_child(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", PATH)
    started: list[DrippedHeader] = []

    with pytest.raises(ForcedFailureError):
        fail_between_the_connections(volume, started)

    [run] = started
    assert not run.thread.is_alive()
    assert run.spawns.still_running() == []


def test_a_stalled_controller_serving_an_idle_connection_exits_one_at_seven_seconds(
    volume: Path,
) -> None:
    controller = subprocess.Popen(
        [sys.executable, "-I", "-c", STALLED_CONTROLLER, str(volume)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        client = connect_when_bound(
            volume, lambda: controller.poll() is None, timeout=WAIT_SECONDS
        )
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
