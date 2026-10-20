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
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from presentator.adapters.worker_children import (
    ChildIdentity,
    GatedChild,
    spawn_gated,
)
from presentator.host import worker, worker_serving
from presentator.host.worker_serving import LifeEnd, Serving, hand_over
from tests.worker_support import RecordingCreation, connect

OWN_IDENTITY = ChildIdentity(os.getuid(), os.getgid(), extra_groups=None)
WAIT_SECONDS = 20
PATH = "/usr/bin:/bin"
STATUS = b"PBW\x01\x03\x00\x02{}"
STEP_HEADER = b'{"step":"build","slug":"my-deck"}'
VALID_STEP = b"PBW\x01\x02" + len(STEP_HEADER).to_bytes(2, "big") + STEP_HEADER
HELLO = b"PBW\x01\x01\x00\x02{}"
FORGER = """
import os, sys
os.pwrite(int(sys.argv[2]), bytes.fromhex(sys.argv[3]), 0)
sys.exit(5)
"""
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
    """The real gated spawn, with a program in place of a role where a test asks."""

    programs: Mapping[str, list[str]] = field(default_factory=dict[str, list[str]])
    roles: list[str] = field(default_factory=list[str])
    children: list[GatedChild] = field(default_factory=list[GatedChild])
    process_ids: list[int] = field(default_factory=list[int])

    def __call__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> GatedChild:
        role = command[command.index("--role") + 1]
        self.roles.append(role)
        program = self.programs.get(role)
        if program is not None:
            source, *extras = program
            command = [sys.executable, "-I", "-c", source, *map(str, pass_fds), *extras]
        child = spawn_gated(
            command, identity=OWN_IDENTITY, environment=environment, pass_fds=pass_fds
        )
        self.children.append(child)
        status = child.status_text()
        self.process_ids.append(int(status.split("Pid:")[1].split()[0]))
        return child

    def kill_and_reap(self) -> None:
        for child in self.children:
            child.kill()
            child.wait()
        for process_id in self.process_ids:
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)


@dataclass
class Life:
    """What one controller life did, as the client and the controller saw it."""

    life_end: LifeEnd | None
    reply: bytes
    spawn: ChainSpawn


class NeverArmed:
    """A watchdog that must not arm a real timer inside pytest."""

    def arm(self, remaining: float) -> None:
        pass

    def cancel(self) -> None:
        pass


def read_to_end(client: socket.socket) -> bytes:
    received = b""
    while chunk := client.recv(4096):
        received += chunk
    return received


def run_life(
    sent: bytes,
    spawn: ChainSpawn,
    seal: Callable[[int], None] = worker.seal_record,
    create_record: Callable[[], int] = worker.create_request_record,
) -> Life:
    controller_end, client = socket.socketpair()
    client.settimeout(WAIT_SECONDS)
    serving = Serving(
        spawn=spawn,
        gate_admits=lambda _status: True,
        sweep=lambda _deadline: True,
        create_record=create_record,
        seal=seal,
        watchdog=NeverArmed(),
        clock=time.monotonic,
        build_timeout_seconds=300.0,
    )
    outcome: list[LifeEnd | None] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            hand_over(controller_end, serving, time.monotonic())
        ),
        daemon=True,
    )
    with closing(controller_end), closing(client):
        try:
            client.sendall(sent)
            thread.start()
            thread.join(WAIT_SECONDS)
            assert not thread.is_alive()
        finally:
            spawn.kill_and_reap()
        controller_end.close()
        return Life(outcome[0], read_to_end(client), spawn)


@pytest.fixture
def path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", PATH)


@pytest.mark.usefixtures("path")
def test_the_upload_child_answers_status_from_the_record_the_idle_child_wrote() -> None:
    written: list[bytes] = []

    def seal_then_read_as_the_test(record: int) -> None:
        worker.seal_record(record)
        written.append(os.pread(record, 4096, 0))

    life = run_life(VALID_STEP, ChainSpawn(), seal_then_read_as_the_test)

    assert life.reply == STATUS
    assert life.life_end == LifeEnd.STEP_ACCEPTED
    assert life.spawn.roles == ["idle", "upload"]
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

    life = run_life(VALID_STEP, ChainSpawn(), refuse)

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
            create_record=creation,
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
from presentator.adapters.worker_children import ChildIdentity, spawn_gated
from presentator.host import worker
from presentator.host.worker_serving import Serving
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
    watchdog=FaultHandlerWatchdog(),
    clock=clock_announcing_each_reading,
    build_timeout_seconds=1.0,
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


def connect_when_serving(volume: Path, controller: GatedChild) -> socket.socket:
    deadline = time.monotonic() + WAIT_SECONDS
    while controller.wait_within(0.05) is None and time.monotonic() < deadline:
        try:
            return connect(volume)
        except (FileNotFoundError, ConnectionRefusedError):
            continue
    pytest.fail("the controller did not start serving")


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
    upload_process_ids: list[int] = []
    try:
        controller.release()
        with closing(connect_when_serving(volume, controller)) as client:
            client.sendall(VALID_STEP)
            exit_status = controller.wait_within(EMERGENCY_END_SECONDS + 30)
            exited_at = time.monotonic()
    finally:
        controller.close_gate()
        controller.kill()
        controller.wait()
        lines = log.read_text().splitlines()
        upload_process_ids = [
            int(line.split()[1]) for line in lines if line.startswith("pid ")
        ]
        for process_id in upload_process_ids:
            with suppress(ProcessLookupError):
                os.kill(process_id, signal.SIGKILL)
        await_absence(upload_process_ids)
    accepted_at = float(lines[0])

    assert len(upload_process_ids) == 1
    assert exit_status == 1
    assert abs(exited_at - accepted_at - EMERGENCY_END_SECONDS) <= TOLERANCE_SECONDS


def await_absence(process_ids: list[int]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and any(
        Path(f"/proc/{process_id}").exists() for process_id in process_ids
    ):
        time.sleep(0.05)
    assert not any(Path(f"/proc/{process_id}").exists() for process_id in process_ids)
