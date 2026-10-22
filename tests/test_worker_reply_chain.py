"""The reply phase through the real roles as child processes."""

import stat
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from presentator.adapters.worker_children import spawn_gated
from presentator.host import worker_serving
from presentator.host.worker_serving import LifeEnd
from presentator.host.worker_wire import WireExit
from tests import step_support
from tests import worker_support as support
from tests.step_support import OWN_IDENTITY
from tests.test_worker_record_chain import (
    PATH,
    SLEEPER,
    WAIT_SECONDS,
    ChainSpawn,
    LifeScenario,
    end_controller,
    logged_process_ids,
    read_to_end,
    run_life,
)

REPLY_BUDGET_SECONDS = 1.0
DECK_MODE_REPORTER_EXITING_SENT = f"""
import os, stat, sys
mode = stat.S_IMODE(os.stat(sys.argv[3]).st_mode)
os.write(int(sys.argv[1]), str(oct(mode)).encode())
sys.exit({int(WireExit.SENT)})
"""
OUTCOME_THEN_STALL = f"""
import os, sys, time
os.write(int(sys.argv[1]), {support.OUTCOME_FAILED!r})
time.sleep(120)
"""
CONSUMING_CONTROLLER = """
import os, sys
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
work = Path(sys.argv[3])
real_spawn = partial(
    spawn_gated, identity=ChildIdentity(os.getuid(), os.getgid(), extra_groups=None)
)

def spawn(command, *, environment, pass_fds):
    child = real_spawn(command, environment=environment, pass_fds=pass_fds)
    role = command[command.index("--role") + 1]
    pid = child.status_text().split("Pid:")[1].split()[0]
    print("pid", role, pid, file=log, flush=True)
    return child

def step_spawn(command, *, environment, pass_fds, surroundings):
    child = real_spawn(
        command,
        environment=environment,
        pass_fds=pass_fds,
        surroundings=surroundings,
    )
    pid = child.status_text().split("Pid:")[1].split()[0]
    print("pid step", pid, file=log, flush=True)
    return child

serving = Serving(
    spawn=spawn,
    gate_admits=lambda _status: True,
    sweep=lambda _deadline: True,
    create_record=worker.create_request_record,
    seal=worker.seal_record,
    open_deck=worker.make_open_directory,
    close_deck=worker.close_deck,
    work=work,
    step=StepEffects(
        spawn=step_spawn,
        gate_admits=lambda _status: True,
        prepare=worker.prepare_step_paths,
        environment={"PATH": os.environ["PATH"], "HOME": str(work / "home")},
        project=work,
    ),
    watchdog=FaultHandlerWatchdog(),
    clock=__import__("time").monotonic,
    readable=readable_within,
    build_timeout_seconds=300.0,
    build_output_megabytes=1,
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


@dataclass(frozen=True)
class ControllerLife:
    """One life of a controller process, as the client and the process showed it."""

    reply: bytes
    exit_status: int | None
    roles: list[str]
    step_pid: int
    announced_pid: int
    present_after_exit: frozenset[int]


def controller_life(volume: Path, workspace: Path, sent: bytes) -> ControllerLife:
    """Run a controller process over a fake pnpm that announces itself and exits 0."""
    log = workspace / "controller.log"
    log.touch()
    work = workspace / "work"
    work.mkdir()
    with step_support.announcements(workspace) as (fifo, reader):
        bin_directory = step_support.fake_pnpm(
            workspace / "bin", step_support.announcing(fifo)
        )
        controller = spawn_gated(
            [
                sys.executable,
                "-I",
                "-c",
                CONSUMING_CONTROLLER,
                str(volume),
                str(log),
                str(work),
            ],
            identity=OWN_IDENTITY,
            environment={"PATH": f"{bin_directory}:{PATH}"},
            pass_fds=(),
        )
        try:
            controller.release()
            with closing(
                support.connect_when_serving(volume, controller, WAIT_SECONDS)
            ) as client:
                client.sendall(sent)
                announced_pid = step_support.await_running_step(reader, WAIT_SECONDS)
                reply = read_to_end(client)
            exit_status = controller.wait_within(WAIT_SECONDS)
            logged = log.read_text().splitlines()
            present = support.present_among(logged_process_ids(logged))
        finally:
            lines = end_controller(controller, log)
    pids = {line.split()[1]: int(line.split()[2]) for line in lines}
    return ControllerLife(
        reply,
        exit_status,
        [line.split()[1] for line in lines],
        pids["step"],
        announced_pid,
        present,
    )


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PATH", PATH)
    return tmp_path


def test_a_valid_upload_over_a_controller_process_gets_status_failed_end_and_eof(
    volume: Path, tmp_path: Path
) -> None:
    life = controller_life(volume, tmp_path, support.VALID_STEP + support.END)

    assert life.reply == support.STATUS + support.OUTCOME_FAILED + support.END
    assert life.exit_status == 0
    assert life.roles == ["idle", "upload", "step", "send"]
    assert life.announced_pid == life.step_pid
    assert not life.present_after_exit


def test_the_deck_a_fake_sender_sees_is_0755_and_its_exit_sent_ends_the_life_consumed(
    sandbox: Path,
) -> None:
    deck = sandbox / "deck"
    spawn = ChainSpawn(programs={"send": [DECK_MODE_REPORTER_EXITING_SENT, str(deck)]})

    life = run_life(support.VALID_STEP + support.END, spawn, LifeScenario(work=sandbox))

    assert life.reply == support.STATUS + oct(support.DECK_CLOSED_MODE).encode()
    assert life.life_end == LifeEnd.CONSUMED


def test_an_exit_that_is_not_sent_ends_the_life_wire_exit_with_the_status_only(
    sandbox: Path,
) -> None:
    spawn = ChainSpawn(programs={"send": ["import sys; sys.exit(1)"]})

    life = run_life(support.VALID_STEP + support.END, spawn, LifeScenario(work=sandbox))

    assert life.reply == support.STATUS
    assert life.life_end == LifeEnd.WIRE_EXIT


STALLED_SENDERS = {
    "a sender that stalls": (SLEEPER, support.STATUS),
    "a sender that writes the OUTCOME and stalls": (
        OUTCOME_THEN_STALL,
        support.STATUS + support.OUTCOME_FAILED,
    ),
}


@pytest.mark.parametrize(
    ("program", "reply"), STALLED_SENDERS.values(), ids=STALLED_SENDERS.keys()
)
def test_a_stalled_sender_is_killed_at_the_reply_budget_leaving_the_reply_incomplete(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, program: str, reply: bytes
) -> None:
    monkeypatch.setattr(worker_serving, "REPLY_SECONDS", REPLY_BUDGET_SECONDS)
    spawn = ChainSpawn(programs={"send": [program]})
    deck = sandbox / "deck"

    started = time.monotonic()
    life = run_life(support.VALID_STEP + support.END, spawn, LifeScenario(work=sandbox))
    elapsed = time.monotonic() - started

    assert life.life_end == LifeEnd.REPLY_EXPIRED
    assert life.reply == reply
    assert REPLY_BUDGET_SECONDS <= elapsed < REPLY_BUDGET_SECONDS + WAIT_SECONDS
    assert stat.S_IMODE(deck.stat().st_mode) == support.DECK_CLOSED_MODE
