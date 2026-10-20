"""The reply phase through the real roles as child processes."""

import stat
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from presentator.adapters.worker_children import spawn_gated
from presentator.host import worker_serving
from presentator.host.worker_serving import LifeEnd
from presentator.host.worker_wire import WireExit
from tests.test_worker_record_chain import (
    END,
    OUTCOME_FAILED,
    OWN_IDENTITY,
    PATH,
    SLEEPER,
    STATUS,
    VALID_STEP,
    WAIT_SECONDS,
    ChainSpawn,
    LifeScenario,
    connect_when_serving,
    end_controller,
    read_to_end,
    run_life,
)
from tests.worker_support import DECK_CLOSED_MODE

REPLY_BUDGET_SECONDS = 1.0
DECK_MODE_REPORTER_EXITING_SENT = f"""
import os, stat, sys
mode = stat.S_IMODE(os.stat(sys.argv[3]).st_mode)
os.write(int(sys.argv[1]), str(oct(mode)).encode())
sys.exit({int(WireExit.SENT)})
"""
OUTCOME_THEN_STALL = f"""
import os, sys, time
os.write(int(sys.argv[1]), {OUTCOME_FAILED!r})
time.sleep(120)
"""
CONSUMING_CONTROLLER = """
import os, sys
from functools import partial
from pathlib import Path
from presentator.adapters.worker_children import ChildIdentity, spawn_gated
from presentator.host import worker
from presentator.host.worker_serving import Serving
from presentator.host.worker_termination import FaultHandlerWatchdog

log = open(sys.argv[2], "a")
deck = Path(sys.argv[3])
real_spawn = partial(
    spawn_gated, identity=ChildIdentity(os.getuid(), os.getgid(), extra_groups=None)
)

def spawn(command, *, environment, pass_fds):
    child = real_spawn(command, environment=environment, pass_fds=pass_fds)
    role = command[command.index("--role") + 1]
    pid = child.status_text().split("Pid:")[1].split()[0]
    print("pid", role, pid, file=log, flush=True)
    return child

serving = Serving(
    spawn=spawn,
    gate_admits=lambda _status: True,
    sweep=lambda _deadline: True,
    create_record=worker.create_request_record,
    seal=worker.seal_record,
    open_deck=partial(worker.open_deck, deck),
    close_deck=partial(worker.close_deck, deck),
    watchdog=FaultHandlerWatchdog(),
    clock=__import__("time").monotonic,
    build_timeout_seconds=300.0,
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


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PATH", PATH)
    return tmp_path


def test_a_valid_upload_over_a_controller_process_gets_status_failed_end_and_eof(
    volume: Path, tmp_path: Path
) -> None:
    log = tmp_path / "controller.log"
    log.touch()
    deck = tmp_path / "deck"
    controller = spawn_gated(
        [
            sys.executable,
            "-I",
            "-c",
            CONSUMING_CONTROLLER,
            str(volume),
            str(log),
            str(deck),
        ],
        identity=OWN_IDENTITY,
        environment={"PATH": PATH},
        pass_fds=(),
    )
    try:
        controller.release()
        with closing(connect_when_serving(volume, controller)) as client:
            client.sendall(VALID_STEP + END)
            reply = read_to_end(client)
        exit_status = controller.wait_within(WAIT_SECONDS)
    finally:
        lines = end_controller(controller, log)

    assert reply == STATUS + OUTCOME_FAILED + END
    assert exit_status == 0
    assert [line.split()[1] for line in lines] == ["idle", "upload", "send"]


def test_the_deck_a_fake_sender_sees_is_0755_and_its_exit_sent_ends_the_life_consumed(
    sandbox: Path,
) -> None:
    deck = sandbox / "deck"
    spawn = ChainSpawn(programs={"send": [DECK_MODE_REPORTER_EXITING_SENT, str(deck)]})

    life = run_life(VALID_STEP + END, spawn, LifeScenario(deck=deck))

    assert life.reply == STATUS + oct(DECK_CLOSED_MODE).encode()
    assert life.life_end == LifeEnd.CONSUMED


def test_an_exit_that_is_not_sent_ends_the_life_wire_exit_with_the_status_only(
    sandbox: Path,
) -> None:
    spawn = ChainSpawn(programs={"send": ["import sys; sys.exit(1)"]})

    life = run_life(VALID_STEP + END, spawn, LifeScenario(deck=sandbox / "deck"))

    assert life.reply == STATUS
    assert life.life_end == LifeEnd.WIRE_EXIT


STALLED_SENDERS = {
    "a sender that stalls": (SLEEPER, STATUS),
    "a sender that writes the OUTCOME and stalls": (
        OUTCOME_THEN_STALL,
        STATUS + OUTCOME_FAILED,
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
    life = run_life(VALID_STEP + END, spawn, LifeScenario(deck=deck))
    elapsed = time.monotonic() - started

    assert life.life_end == LifeEnd.REPLY_EXPIRED
    assert life.reply == reply
    assert REPLY_BUDGET_SECONDS <= elapsed < REPLY_BUDGET_SECONDS + WAIT_SECONDS
    assert stat.S_IMODE(deck.stat().st_mode) == DECK_CLOSED_MODE
