"""The request record through the real idle and upload roles as child processes."""

import errno
import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, closing, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from presentator.adapters.worker_children import (
    ChildSurroundings,
    GatedChild,
    readable_within,
    spawn_gated,
)
from presentator.host import worker, worker_serving
from presentator.host.worker_serving import (
    LifeEnd,
    Serving,
    StepEffects,
    WireChild,
    hand_over,
)
from presentator.host.worker_wire import WireExit
from tests.step_support import OWN_IDENTITY
from tests.worker_support import (
    END,
    STATUS,
    STEP_HEADER,
    VALID_STEP,
    NeverArmed,
    RecordingCreation,
    assert_gone,
    connect_when_serving,
    listening_socket,
    present_among,
    process_id_of,
    role_of,
)

WAIT_SECONDS = 20
PATH = "/usr/bin:/bin"
FORGER = """
import os, sys
os.pwrite(int(sys.argv[2]), bytes.fromhex(sys.argv[3]), 0)
sys.exit(5)
"""
EXITING_SENT = f"import sys; sys.exit({int(WireExit.SENT)})"
EXITING_SEVEN = "import sys; sys.exit(7)"
EXITING_ZERO = "import sys; sys.exit(0)"
SLEEPER = "import time; time.sleep(120)"
INODES_REPORTER = """
import json, os, sys
identities = []
for name in os.listdir("/proc/self/fd"):
    try:
        held = os.stat("/proc/self/fd/" + name)
    except OSError:
        continue
    identities.append([held.st_dev, held.st_ino])
os.write(int(sys.argv[1]), json.dumps(identities).encode())
sys.exit(4)
"""


@dataclass
class ChainSpawn:
    """The real gated spawn, with a program in place of a role where a test asks.

    The step exits 0 at once unless a test gives it a program or asks for the
    real launcher.
    """

    programs: Mapping[str, list[str]] = field(default_factory=dict[str, list[str]])
    real_launcher: bool = False
    sender_gate: Callable[[GatedChild], WireChild] = lambda child: child
    roles: list[str] = field(default_factory=list[str])
    children: list[GatedChild] = field(default_factory=list[GatedChild])
    process_ids: list[int] = field(default_factory=list[int])

    def __call__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> WireChild:
        role = role_of(command)
        child = self._started(
            role,
            self._substituted(role, command, pass_fds),
            environment,
            pass_fds,
            ChildSurroundings(),
        )
        return self.sender_gate(child) if role == "send" else child

    def step(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
        surroundings: ChildSurroundings,
    ) -> GatedChild:
        return self._started(
            "step",
            self._substituted("step", command, pass_fds),
            environment,
            pass_fds,
            surroundings,
        )

    def _substituted(
        self, role: str, command: Sequence[str], pass_fds: Sequence[int]
    ) -> Sequence[str]:
        program = self.programs.get(role)
        if program is None and role == "step" and not self.real_launcher:
            program = [EXITING_ZERO]
        if program is None:
            return command
        source, *extras = program
        return [sys.executable, "-I", "-c", source, *map(str, pass_fds), *extras]

    def _started(
        self,
        role: str,
        command: Sequence[str],
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
        surroundings: ChildSurroundings,
    ) -> GatedChild:
        self.roles.append(role)
        child = spawn_gated(
            command,
            identity=OWN_IDENTITY,
            environment=environment,
            pass_fds=pass_fds,
            surroundings=surroundings,
        )
        self.children.append(child)
        self.process_ids.append(process_id_of(child))
        return child

    def kill_and_reap(self) -> None:
        for child in self.children:
            child.kill()
        unreaped = [
            child for child in self.children if child.wait_within(WAIT_SECONDS) is None
        ]
        assert not unreaped, "a killed child was not reaped"
        assert_gone(self.process_ids)


@dataclass
class Life:
    """What one controller life did, as the client and the controller saw it."""

    life_end: LifeEnd | None
    reply: bytes
    spawn: ChainSpawn
    started_at: float
    returned_at: float
    present_at_return: frozenset[int]


def read_to_end(client: socket.socket) -> bytes:
    """Read until a clean EOF; a reset raises."""
    received = b""
    while chunk := client.recv(4096):
        received += chunk
    return received


def read_to_end_or_reset(client: socket.socket) -> bytes:
    """Read until EOF or a reset, for refusals that leave bytes unread."""
    received = b""
    with suppress(ConnectionResetError):
        while chunk := client.recv(4096):
            received += chunk
    return received


@dataclass(frozen=True)
class LifeScenario:
    """What a life runs on besides the spawn: record effects, places and reader."""

    seal: Callable[[int], None] = worker.seal_record
    create_record: Callable[[], int] = worker.create_request_record
    work: Path | None = None
    project: Path | None = None
    build_timeout_seconds: float = 300.0
    reader: Callable[[socket.socket], bytes] = read_to_end
    listener: socket.socket | None = None


def chain_environment(work: Path) -> dict[str, str]:
    """The six-key step environment, with test values below the work root."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(work / worker.HOME_DIRECTORY),
        "TMPDIR": str(work),
        "PLAYWRIGHT_BROWSERS_PATH": str(work / "playwright"),
        "COREPACK_HOME": str(work / "corepack"),
        "COREPACK_ENABLE_NETWORK": "0",
    }


def run_life(
    sent: bytes,
    spawn: ChainSpawn,
    scenario: LifeScenario | None = None,
    converse: Callable[[socket.socket], None] = lambda _client: None,
) -> Life:
    scenario = scenario or LifeScenario()
    with ExitStack() as cleanup:
        work = scenario.work or Path(cleanup.enter_context(TemporaryDirectory()))
        project = scenario.project or Path(cleanup.enter_context(TemporaryDirectory()))
        listener = scenario.listener or cleanup.enter_context(listening_socket())
        return _run_life_at(
            sent,
            spawn,
            replace(scenario, work=work, project=project, listener=listener),
            converse,
        )


def _end_life(
    spawn: ChainSpawn, thread: threading.Thread, client: socket.socket
) -> None:
    try:
        client.close()
    finally:
        try:
            spawn.kill_and_reap()
        finally:
            thread.join(WAIT_SECONDS)
            assert not thread.is_alive(), "the controller outlived its life"


def _run_life_at(
    sent: bytes,
    spawn: ChainSpawn,
    scenario: LifeScenario,
    converse: Callable[[socket.socket], None],
) -> Life:
    work, project, listener = scenario.work, scenario.project, scenario.listener
    assert work is not None
    assert project is not None
    assert listener is not None
    controller_end, client = socket.socketpair()
    client.settimeout(WAIT_SECONDS)
    serving = Serving(
        spawn=spawn,
        gate_admits=lambda _status: True,
        sweep=lambda _deadline: True,
        create_record=scenario.create_record,
        seal=scenario.seal,
        open_deck=worker.make_open_directory,
        close_deck=worker.close_deck,
        work=work,
        step=StepEffects(
            spawn=spawn.step,
            gate_admits=lambda _status: True,
            prepare=worker.prepare_step_paths,
            environment=chain_environment(work),
            project=project,
        ),
        watchdog=NeverArmed(),
        clock=time.monotonic,
        readable=readable_within,
        build_timeout_seconds=scenario.build_timeout_seconds,
        build_output_megabytes=300,
    )
    outcome: list[LifeEnd | None] = []
    returned_at: list[float] = []

    def control() -> None:
        outcome.append(
            hand_over(controller_end, serving, time.monotonic(), listener=listener)
        )
        returned_at.append(time.monotonic())

    thread = threading.Thread(target=control, daemon=True)
    with closing(controller_end), closing(client):
        try:
            started_at = time.monotonic()
            thread.start()
            client.sendall(sent)
            converse(client)
            thread.join(WAIT_SECONDS)
            present = present_among(spawn.process_ids)
            controller_end.close()
            reply = scenario.reader(client)
        finally:
            _end_life(spawn, thread, client)
        return Life(outcome[0], reply, spawn, started_at, returned_at[0], present)


@pytest.fixture
def path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", PATH)


@pytest.mark.usefixtures("path")
def test_the_upload_child_answers_status_from_the_record_the_idle_child_wrote() -> None:
    written: list[bytes] = []

    def seal_then_read_as_the_test(record: int) -> None:
        worker.seal_record(record)
        written.append(os.pread(record, 4096, 0))

    life = run_life(
        VALID_STEP + END,
        ChainSpawn(programs={"send": [EXITING_SENT]}),
        LifeScenario(seal=seal_then_read_as_the_test),
    )

    assert life.reply == STATUS
    assert life.life_end == LifeEnd.CONSUMED
    assert life.spawn.roles == ["idle", "upload", "step", "send"]
    assert written == [STEP_HEADER]


FORGED_RECORDS = {
    "empty": b"",
    "truncated": STEP_HEADER[:-4],
    "noise": b"\x00\xff\x10 noise",
    "an unknown step": b'{"step":"deploy","slug":"my-deck"}',
    "slug ..": b'{"step":"build","slug":".."}',
    "a slug with a slash": b'{"step":"build","slug":"a/b"}',
}


@pytest.mark.usefixtures("path")
@pytest.mark.parametrize("forged", FORGED_RECORDS.values(), ids=FORGED_RECORDS.keys())
def test_an_undecodable_record_closes_the_upload_child_before_status_and_ends_the_life(
    forged: bytes,
) -> None:
    spawn = ChainSpawn(programs={"idle": [FORGER, forged.hex()]})
    life = run_life(b"", spawn)

    assert life.reply == b""
    assert life.life_end == LifeEnd.WIRE_EXIT
    assert spawn.roles == ["idle", "upload"]


@pytest.mark.usefixtures("path")
def test_a_failing_seal_closes_without_status_and_spawns_no_upload_child() -> None:
    def refuse(_record: int) -> None:
        raise OSError(errno.EBUSY, "a writable shared mapping exists")

    life = run_life(VALID_STEP, ChainSpawn(), LifeScenario(seal=refuse))

    assert life.reply == b""
    assert life.life_end == LifeEnd.SEAL_FAILED
    assert life.spawn.roles == ["idle"]


@pytest.mark.usefixtures("path")
def test_a_stalled_upload_child_is_killed_at_the_end_of_its_budget_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_serving, "UPLOAD_SECONDS", 1.0)
    spawn = ChainSpawn(programs={"upload": [SLEEPER]})

    started = time.monotonic()
    life = run_life(VALID_STEP, spawn)

    assert life.reply == b""
    assert life.life_end == LifeEnd.UPLOAD_EXPIRED
    assert 1.0 <= time.monotonic() - started < 1.0 + WAIT_SECONDS / 2
    assert spawn.roles == ["idle", "upload"]


@pytest.mark.usefixtures("path")
def test_the_next_life_does_not_hold_the_record_of_the_one_before() -> None:
    creation = RecordingCreation()
    records: list[tuple[int, int]] = []
    held_by_child: list[set[tuple[int, int]]] = []
    for _life in range(2):
        life = run_life(
            b"",
            ChainSpawn(programs={"idle": [INODES_REPORTER]}),
            LifeScenario(create_record=creation),
        )
        records.append(creation.identity)
        held_by_child.append({tuple(held) for held in json.loads(life.reply)})
        assert creation.is_closed()

    assert records[0] != records[1]
    assert records[0] in held_by_child[0]
    assert records[1] in held_by_child[1]
    assert records[0] not in held_by_child[1]


STALLED_AFTER_UPLOAD_SPAWN = """
import ctypes, os, sys, time
from functools import partial
from pathlib import Path
from presentator.adapters.worker_children import (
    ChildIdentity,
    readable_within,
    spawn_gated,
)
from presentator.host import worker
from presentator.host.worker_serving import Serving, StepEffects
from presentator.host.worker_termination import FaultHandlerWatchdog

log = open(sys.argv[2], "a")
real_spawn = partial(
    spawn_gated, identity=ChildIdentity(os.getuid(), os.getgid(), extra_groups=None)
)

def spawn(command, *, environment, pass_fds):
    child = real_spawn(command, environment=environment, pass_fds=pass_fds)
    if "upload" in command:
        pid = child.status_text().split("Pid:")[1].split()[0]
        print("pid", pid, file=log, flush=True)
        ctypes.PyDLL(None).pause()
    return child

def refuse_step(*_arguments, **_keywords):
    raise AssertionError("the step is not part of this test")

def clock_announcing_each_reading():
    reading = time.monotonic()
    print(reading, file=log, flush=True)
    return reading

serving = Serving(
    spawn=spawn,
    gate_admits=lambda _status: True,
    sweep=lambda _deadline: True,
    create_record=worker.create_request_record,
    seal=worker.seal_record,
    open_deck=lambda _deck: None,
    close_deck=lambda _deck: None,
    work=Path("/unused/work"),
    step=StepEffects(
        spawn=refuse_step,
        gate_admits=lambda _status: False,
        prepare=refuse_step,
        environment={},
        project=Path("/unused/project"),
    ),
    watchdog=FaultHandlerWatchdog(),
    clock=clock_announcing_each_reading,
    readable=readable_within,
    build_timeout_seconds=1.0,
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
EMERGENCY_END_SECONDS = 148.0
TOLERANCE_SECONDS = 1.0


def test_a_controller_that_stalls_after_the_upload_spawn_dies_at_the_emergency_end(
    volume: Path, tmp_path: Path
) -> None:
    log = tmp_path / "controller.log"
    log.touch()
    controller = spawn_gated(
        [sys.executable, "-I", "-c", STALLED_AFTER_UPLOAD_SPAWN, str(volume), str(log)],
        identity=OWN_IDENTITY,
        environment={"PATH": PATH},
        pass_fds=(),
    )
    try:
        controller.release()
        with closing(connect_when_serving(volume, controller, WAIT_SECONDS)) as client:
            client.sendall(VALID_STEP)
            exit_status = controller.wait_within(EMERGENCY_END_SECONDS + 30)
            exited_at = time.monotonic()
    finally:
        lines = end_controller(controller, log)
    upload_process_ids = logged_process_ids(lines)
    accepted_at = float(lines[0])

    assert len(upload_process_ids) == 1
    assert exit_status == 1
    assert abs(exited_at - accepted_at - EMERGENCY_END_SECONDS) <= TOLERANCE_SECONDS


def logged_process_ids(lines: list[str]) -> list[int]:
    return [int(line.split()[-1]) for line in lines if line.startswith("pid ")]


def end_controller(controller: GatedChild, log: Path) -> list[str]:
    """Stop the controller and SIGKILL every wire child it logged, even on a stall."""
    try:
        controller.close()
        controller.kill()
        assert controller.wait_within(WAIT_SECONDS) is not None, "controller not reaped"
    finally:
        lines = log.read_text().splitlines()
        process_ids = logged_process_ids(lines)
        for process_id in process_ids:
            with suppress(ProcessLookupError):
                os.kill(process_id, signal.SIGKILL)
        await_absence(process_ids)
    return lines


def await_absence(process_ids: list[int]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and any(
        Path(f"/proc/{process_id}").exists() for process_id in process_ids
    ):
        time.sleep(0.05)
    assert not any(Path(f"/proc/{process_id}").exists() for process_id in process_ids)
