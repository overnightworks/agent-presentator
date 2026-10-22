"""The step through the real roles and the real launcher as child processes."""

import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from presentator.adapters.worker_children import GatedChild
from presentator.contracts.decks import FAILURE_TEXT_LIMIT
from presentator.host import worker_serving
from presentator.host.worker_serving import LifeEnd
from presentator.host.worker_wire import WireExit
from tests import step_support
from tests import worker_support as support
from tests.test_worker_record_chain import (
    PATH,
    WAIT_SECONDS,
    ChainSpawn,
    Life,
    LifeScenario,
    chain_environment,
    read_to_end,
    read_to_end_or_reset,
    run_life,
)

BUILD_BOUND_SECONDS = 3.0
INJECTION_DEADLINE_SECONDS = 2.0
STEP_ERROR_CHARACTERS = 3000
SENT = support.VALID_STEP + support.END
REPLY = support.STATUS + support.OUTCOME_FAILED + support.END
SLEEPING = "import time\ntime.sleep(120)\n"
SLEEPING_PAST_SIGTERM = (
    "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + SLEEPING
)
REPORTING_PNPM = """
import json, os, sys
descriptors = {{
    int(name): os.readlink("/proc/self/fd/" + name)
    for name in os.listdir("/proc/self/fd")
    if os.path.exists("/proc/self/fd/" + name)
}}
os.makedirs({talk!r})
open({talk!r} + "/index.html", "w").write("built")
open({report!r}, "w").write(
    json.dumps(
        {{
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "environment": dict(os.environ),
            "descriptors": descriptors,
        }}
    )
)
sys.stderr.write("x" * {characters})
print("STEP-STDOUT")
"""
FAILING_PNPM = """
import sys
sys.stderr.write("a" * {older} + "b" * {tail})
sys.exit(1)
"""
PLANTING_UPLOAD = f"""
import os, sys
os.write(int(sys.argv[1]), {support.STATUS!r})
target = sys.argv[3]
%s
sys.exit({int(WireExit.UPLOADED)})
"""
PLANTS = {
    "a directory": "os.mkdir(target)",
    "a symlink to an outside directory": "os.symlink(sys.argv[4], target)",
}
EXTRA_ENVIRONMENT_KEYS = {"PWD", "LC_CTYPE"}


class ActingChild:
    """A sender at its gate that runs an action just before one of its gate calls."""

    def __init__(
        self, child: GatedChild, act_when: str, act: Callable[[], None]
    ) -> None:
        """Wrap the child; the action runs before the named call."""
        self._child = child
        self._act_when = act_when
        self._act = act

    def status_text(self) -> str:
        if self._act_when == "status_text":
            self._act()
        return self._child.status_text()

    def release(self) -> None:
        if self._act_when == "release":
            self._act()
        self._child.release()

    def kill(self) -> None:
        self._child.kill()

    def wait(self) -> int:
        return self._child.wait()

    def wait_within(self, seconds: float) -> int | None:
        return self._child.wait_within(seconds)

    @property
    def exit_descriptor(self) -> int:
        return self._child.exit_descriptor

    def close(self) -> None:
        self._child.close()


@dataclass
class Client:
    """The test's end of a life's connection, known once the life has started."""

    ready: threading.Event = field(default_factory=threading.Event)
    held: socket.socket | None = None

    def hold(self, client: socket.socket) -> None:
        self.held = client
        self.ready.set()

    def act(self, action: Callable[[socket.socket], None]) -> None:
        assert self.ready.wait(WAIT_SECONDS), "the life never gave the client"
        assert self.held is not None
        action(self.held)


def send_a_byte(client: socket.socket) -> None:
    client.sendall(b"x")


def half_close(client: socket.socket) -> None:
    client.shutdown(socket.SHUT_WR)


def close(client: socket.socket) -> None:
    client.close()


def nothing_to_read(_client: socket.socket) -> bytes:
    return b""


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PATH", PATH)
    return tmp_path


def sender_pid(life: Life) -> int:
    return life.spawn.process_ids[life.spawn.roles.index("send")]


def step_pid(life: Life) -> int:
    return life.spawn.process_ids[life.spawn.roles.index("step")]


BEFORE_THE_CHECK = {
    "a byte": (send_a_byte, read_to_end_or_reset),
    "a half-close": (half_close, read_to_end),
}


@pytest.mark.parametrize(
    ("event", "reader"), BEFORE_THE_CHECK.values(), ids=BEFORE_THE_CHECK.keys()
)
def test_a_client_event_before_the_last_check_kills_the_gated_sender_and_cancels(
    sandbox: Path,
    event: Callable[[socket.socket], None],
    reader: Callable[[socket.socket], bytes],
) -> None:
    client = Client()
    spawn = ChainSpawn(
        sender_gate=lambda child: ActingChild(
            child, "status_text", lambda: client.act(event)
        )
    )

    life = run_life(SENT, spawn, LifeScenario(work=sandbox, reader=reader), client.hold)

    assert life.life_end == LifeEnd.CANCELLED
    assert sender_pid(life) not in life.present_at_return
    assert spawn.roles == ["idle", "upload", "step", "send"]
    assert life.reply == support.STATUS


def test_a_half_close_after_the_last_check_does_not_cancel_the_reply(
    sandbox: Path,
) -> None:
    client = Client()
    spawn = ChainSpawn(
        sender_gate=lambda child: ActingChild(
            child, "release", lambda: client.act(half_close)
        )
    )

    life = run_life(SENT, spawn, LifeScenario(work=sandbox), client.hold)

    assert life.life_end == LifeEnd.CONSUMED
    assert life.reply == support.STATUS + support.OUTCOME_REFUSED_SHAPE + support.END


def test_an_export_writing_its_pdf_gets_built_and_the_pdf_as_the_only_entry(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = sandbox / "work"
    work.mkdir()
    pnpm = step_support.fake_pnpm(
        sandbox / "bin",
        f"open({str(work / 'out' / 'deck.pdf')!r}, 'wb').write(b'%PDF')\n",
    )
    monkeypatch.setenv("PATH", f"{pnpm}:{PATH}")
    export = support.framed(b'{"step":"export","slug":"my-deck"}', support.STEP)

    life = run_life(
        export + support.END,
        ChainSpawn(real_launcher=True),
        LifeScenario(work=work, project=sandbox),
    )

    assert life.reply == (
        support.STATUS
        + support.OUTCOME_BUILT
        + support.file_entry(b"deck.pdf", b"%PDF")
        + support.end_entry(1, 4)
    )


BLOB_BYTES = 16 * 1024 * 1024
REPLY_BOUND_SECONDS = 1.0
# The gate records the sender's spawn just after the controller's own clock began.
SPAWN_TOLERANCE_SECONDS = 0.1


def a_blob_building_chain(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ChainSpawn, list[float]]:
    """A real-launcher chain whose build writes a blob no socket buffer holds."""
    work = sandbox / "work"
    work.mkdir()
    talk = work / "out" / "talk"
    pnpm = step_support.fake_pnpm(
        sandbox / "bin",
        f"import os\nos.makedirs({str(talk)!r})\n"
        f"open({str(talk / 'blob')!r}, 'wb').truncate({BLOB_BYTES})\n",
    )
    monkeypatch.setenv("PATH", f"{pnpm}:{PATH}")
    sender_spawned: list[float] = []
    spawn = ChainSpawn(
        real_launcher=True,
        sender_gate=lambda child: sender_spawned.append(time.monotonic()) or child,
    )
    return work, spawn, sender_spawned


def read_the_first(count: int) -> Callable[[socket.socket], None]:
    def read(client: socket.socket) -> None:
        received = 0
        while received < count:
            received += len(client.recv(count - received))

    return read


def test_a_client_that_stops_reading_a_large_reply_ends_the_life_at_the_reply_bound(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, spawn, sender_spawned = a_blob_building_chain(sandbox, monkeypatch)
    monkeypatch.setattr(worker_serving, "REPLY_SECONDS", REPLY_BOUND_SECONDS)

    life = run_life(
        SENT,
        spawn,
        LifeScenario(work=work, project=sandbox, reader=read_to_end_or_reset),
        read_the_first(len(support.STATUS)),
    )

    assert life.life_end == LifeEnd.REPLY_EXPIRED
    elapsed = life.returned_at - sender_spawned[0]
    assert elapsed >= REPLY_BOUND_SECONDS - SPAWN_TOLERANCE_SECONDS
    assert elapsed < REPLY_BOUND_SECONDS + WAIT_SECONDS
    assert sender_pid(life) not in life.present_at_return
    assert life.reply.startswith(support.STATUS + support.OUTCOME_BUILT)
    assert not life.reply.endswith(support.END)


def test_a_client_that_closes_during_a_large_reply_ends_the_sender_promptly(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, spawn, _ = a_blob_building_chain(sandbox, monkeypatch)
    closed_at: list[float] = []

    def close_after_the_outcome(client: socket.socket) -> None:
        read_the_first(len(support.STATUS + support.OUTCOME_BUILT))(client)
        closed_at.append(time.monotonic())
        client.close()

    life = run_life(
        SENT,
        spawn,
        LifeScenario(work=work, project=sandbox, reader=nothing_to_read),
        close_after_the_outcome,
    )

    assert life.life_end == LifeEnd.WIRE_EXIT
    assert life.returned_at - closed_at[0] < WAIT_SECONDS
    assert sender_pid(life) not in life.present_at_return


def test_a_step_ignoring_sigterm_is_killed_at_the_build_bound_and_the_reply_comes(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    announced: list[int] = []
    with step_support.announcements(sandbox) as (fifo, reader):
        pnpm = step_support.fake_pnpm(
            sandbox / "bin", step_support.announcing(fifo) + SLEEPING_PAST_SIGTERM
        )
        monkeypatch.setenv("PATH", f"{pnpm}:{PATH}")
        spawn = ChainSpawn(real_launcher=True)

        life = run_life(
            SENT,
            spawn,
            LifeScenario(work=sandbox, build_timeout_seconds=BUILD_BOUND_SECONDS),
            lambda _client: announced.append(
                step_support.await_running_step(reader, WAIT_SECONDS)
            ),
        )

    assert announced == [step_pid(life)]
    assert (
        BUILD_BOUND_SECONDS
        <= life.returned_at - life.started_at
        < BUILD_BOUND_SECONDS + WAIT_SECONDS
    )
    assert step_pid(life) not in life.present_at_return
    assert life.reply == REPLY


DURING_THE_BUILD = {
    "a hang-up": (close, nothing_to_read, b""),
    "a half-close": (half_close, read_to_end, support.STATUS),
    "a byte": (send_a_byte, read_to_end_or_reset, support.STATUS),
}


@pytest.mark.parametrize(
    ("event", "reader", "reply"), DURING_THE_BUILD.values(), ids=DURING_THE_BUILD.keys()
)
def test_a_client_event_during_the_build_kills_the_running_step_and_sends_no_reply(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: Callable[[socket.socket], None],
    reader: Callable[[socket.socket], bytes],
    reply: bytes,
) -> None:
    injected_at: list[float] = []
    announced: list[int] = []
    with step_support.announcements(sandbox) as (fifo, running):
        pnpm = step_support.fake_pnpm(
            sandbox / "bin", step_support.announcing(fifo) + SLEEPING
        )
        monkeypatch.setenv("PATH", f"{pnpm}:{PATH}")
        spawn = ChainSpawn(real_launcher=True)

        def inject_once_running(client: socket.socket) -> None:
            announced.append(step_support.await_running_step(running, WAIT_SECONDS))
            injected_at.append(time.monotonic())
            event(client)

        life = run_life(
            SENT, spawn, LifeScenario(work=sandbox, reader=reader), inject_once_running
        )

    assert life.life_end == LifeEnd.CANCELLED
    assert announced == [step_pid(life)]
    assert life.returned_at - injected_at[0] < INJECTION_DEADLINE_SECONDS
    assert step_pid(life) not in life.present_at_return
    assert spawn.roles == ["idle", "upload", "step"]
    assert life.reply == reply


def test_a_step_runs_the_launcher_with_its_paths_environment_and_streams(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    work = sandbox / "work"
    work.mkdir()
    project = sandbox / "project"
    project.mkdir()
    report = sandbox / "report.json"
    program = REPORTING_PNPM.format(
        talk=str(work / "out" / "talk"),
        report=str(report),
        characters=STEP_ERROR_CHARACTERS,
    )
    pnpm = step_support.fake_pnpm(sandbox / "bin", program)
    monkeypatch.setenv("PATH", f"{pnpm}:{PATH}")
    spawn = ChainSpawn(real_launcher=True)

    life = run_life(SENT, spawn, LifeScenario(work=work, project=project))

    seen = json.loads(report.read_text())
    captured = capfd.readouterr()
    assert life.reply == (
        support.STATUS + support.OUTCOME_BUILT + support.talk_entries(b"built")
    )
    assert spawn.roles == ["idle", "upload", "step", "send"]
    assert seen["argv"] == [
        "exec",
        "slidev",
        "build",
        f"{work}/deck/slides.md",
        "--base",
        "/deck/my-deck/",
        "--out",
        f"{work}/out/talk",
    ]
    assert seen["cwd"] == str(project)
    environment = {
        key: value
        for key, value in seen["environment"].items()
        if key not in EXTRA_ENVIRONMENT_KEYS
    }
    assert environment == chain_environment(work)
    assert seen["descriptors"] == {
        "0": "/dev/null",
        "1": "/dev/null",
        "2": str(work / "step.err"),
    }
    assert (work / "step.err").read_text() == "x" * STEP_ERROR_CHARACTERS
    assert "xxxxxxxxxx" not in captured.err
    assert "STEP-STDOUT" not in captured.out + captured.err
    assert (work / "out" / "talk" / "index.html").read_text() == "built"


def test_a_failing_step_is_answered_with_the_last_characters_of_its_stderr_only(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    work = sandbox / "work"
    work.mkdir()
    project = sandbox / "project"
    project.mkdir()
    pnpm = step_support.fake_pnpm(
        sandbox / "bin", FAILING_PNPM.format(older=1000, tail=FAILURE_TEXT_LIMIT)
    )
    monkeypatch.setenv("PATH", f"{pnpm}:{PATH}")
    spawn = ChainSpawn(real_launcher=True)

    life = run_life(SENT, spawn, LifeScenario(work=work, project=project))

    captured = capfd.readouterr()
    tail = b"b" * FAILURE_TEXT_LIMIT
    outcome = support.framed(
        b'{"outcome":"failed","text":"' + tail + b'"}', b"PBW\x01\x05"
    )
    assert life.life_end == LifeEnd.CONSUMED
    assert spawn.roles == ["idle", "upload", "step", "send"]
    assert life.reply == support.STATUS + outcome + support.END
    assert "bbbbbbbbbb" not in captured.err


@pytest.mark.parametrize("plant", PLANTS.values(), ids=PLANTS.keys())
def test_a_node_modules_left_by_the_upload_child_spawns_no_step_and_still_replies(
    sandbox: Path, plant: str
) -> None:
    work = sandbox / "work"
    work.mkdir()
    outside = sandbox / "outside"
    outside.mkdir()
    deck = work / "deck"
    spawn = ChainSpawn(
        programs={
            "upload": [
                PLANTING_UPLOAD % plant,
                str(deck / "node_modules"),
                str(outside),
            ]
        }
    )

    life = run_life(support.VALID_STEP, spawn, LifeScenario(work=work))

    assert life.reply == REPLY
    assert life.life_end == LifeEnd.CONSUMED
    assert spawn.roles == ["idle", "upload", "send"]
    assert list(outside.iterdir()) == []
