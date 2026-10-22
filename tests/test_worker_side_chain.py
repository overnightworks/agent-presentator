"""A side connection during the upload or the reply, over the real child roles."""

import json
import socket
from contextlib import closing
from pathlib import Path

import pytest

from presentator.host.worker_serving import LifeEnd
from presentator.host.worker_wire import WireExit
from tests.test_worker_record_chain import (
    PATH,
    WAIT_SECONDS,
    ChainSpawn,
    LifeScenario,
    read_to_end_or_reset,
    run_life,
)
from tests.worker_support import END, OUTCOME_FAILED, STATUS, VALID_STEP

SENDING_AFTER_THE_SIDE_CLIENT = f"""
import os, sys, time
connection = int(sys.argv[1])
os.write(connection, {OUTCOME_FAILED!r})
marker = sys.argv[3]
deadline = time.monotonic() + {WAIT_SECONDS}
while not os.path.exists(marker) and time.monotonic() < deadline:
    time.sleep(0.01)
os.write(connection, {END!r})
sys.exit({int(WireExit.SENT)})
"""
EXITING_UPLOADED = f"import sys; sys.exit({int(WireExit.UPLOADED)})"
DESCRIPTOR_REPORT = """
import json, os, sys
targets = {}
for name in os.listdir("/proc/self/fd"):
    path = "/proc/self/fd/" + name
    if os.path.exists(path):
        targets[name] = os.readlink(path)
os.write(
    int(sys.argv[1]),
    json.dumps(
        {
            "held": sorted(map(int, targets)),
            "passed": [int(sys.argv[1]), int(sys.argv[2])],
            "targets": targets,
        }
    ).encode(),
)
sys.exit(%d)
"""
INHERITED_DESCRIPTORS = {0, 1, 2}


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PATH", PATH)
    return tmp_path


def read_exactly(client: socket.socket, length: int) -> bytes:
    received = b""
    while len(received) < length:
        chunk = client.recv(length - len(received))
        assert chunk, "the controller closed the main connection early"
        received += chunk
    return received


def a_side_client_is_closed_unanswered(listener: socket.socket) -> None:
    side_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    side_client.settimeout(WAIT_SECONDS)
    with closing(side_client):
        side_client.connect(listener.getsockname())
        assert read_to_end_or_reset(side_client) == b""


def test_a_side_client_during_the_upload_is_closed_while_the_main_reply_completes(
    sandbox: Path, listener: socket.socket
) -> None:
    spawn = ChainSpawn()
    heard: list[bytes] = []

    def side_client_between_status_and_end(client: socket.socket) -> None:
        heard.append(read_exactly(client, len(STATUS)))
        a_side_client_is_closed_unanswered(listener)
        client.sendall(END)

    life = run_life(
        VALID_STEP,
        spawn,
        LifeScenario(deck=sandbox / "deck", listener=listener),
        side_client_between_status_and_end,
    )

    assert heard == [STATUS]
    assert life.reply == OUTCOME_FAILED + END
    assert life.life_end == LifeEnd.CONSUMED
    assert spawn.roles == ["idle", "upload", "send"]


def test_a_side_client_during_the_reply_is_closed_while_the_main_reply_completes(
    sandbox: Path, listener: socket.socket
) -> None:
    marker = sandbox / "side-client-done"
    spawn = ChainSpawn(programs={"send": [SENDING_AFTER_THE_SIDE_CLIENT, str(marker)]})
    heard: list[bytes] = []

    def side_client_between_outcome_and_end(client: socket.socket) -> None:
        heard.append(read_exactly(client, len(STATUS + OUTCOME_FAILED)))
        a_side_client_is_closed_unanswered(listener)
        marker.touch()

    life = run_life(
        VALID_STEP + END,
        spawn,
        LifeScenario(deck=sandbox / "deck", listener=listener),
        side_client_between_outcome_and_end,
    )

    assert heard == [STATUS + OUTCOME_FAILED]
    assert life.reply == END
    assert life.life_end == LifeEnd.CONSUMED
    assert spawn.roles == ["idle", "upload", "send"]


@pytest.mark.parametrize("role", ["upload", "send"])
def test_the_upload_child_and_the_sender_hold_no_pidfd_and_only_their_descriptors(
    role: str, sandbox: Path, listener: socket.socket
) -> None:
    reporting = {"upload": [DESCRIPTOR_REPORT % WireExit.UPLOADED]}
    if role == "send":
        reporting = {
            "upload": [EXITING_UPLOADED],
            "send": [DESCRIPTOR_REPORT % WireExit.SENT],
        }
    spawn = ChainSpawn(programs=reporting)

    life = run_life(
        VALID_STEP + END,
        spawn,
        LifeScenario(
            deck=sandbox / "deck", listener=listener, reader=read_to_end_or_reset
        ),
    )

    report, _rest = json.JSONDecoder().raw_decode(life.reply.decode())
    assert set(report["held"]) == INHERITED_DESCRIPTORS | set(report["passed"])
    assert not any("pidfd" in target for target in report["targets"].values())
