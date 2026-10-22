"""DIR and FILE entries through the real roles: the upload child writes the deck."""

import os
import socket
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

from presentator.adapters.build_wire import MEGABYTE, RecordTag
from presentator.adapters.worker_children import GatedChild, spawn_gated
from presentator.host.worker_serving import LifeEnd
from tests import step_support
from tests import worker_support as support
from tests.step_support import OWN_IDENTITY
from tests.test_worker_record_chain import (
    EXITING_SENT,
    PATH,
    WAIT_SECONDS,
    ChainSpawn,
    LifeScenario,
    end_controller,
    read_to_end,
    read_to_end_or_reset,
    run_life,
)
from tests.test_worker_reply_chain import CONSUMING_CONTROLLER, controller_life
from tests.worker_support import (
    OWN_UID,
    directory_entry,
    end_entry,
    entries_at_the_maximum,
    file_entry,
    file_header,
)

CONTROLLER_UMASK = 0o077
DIRECTORY_MODE = 0o755
FILE_MODE = 0o644
STEP_SCRATCH_MODE = 0o1777
LARGE_FILE = b"x" * (200 * 1024)
NESTED_UPLOAD = (
    directory_entry(b"a")
    + directory_entry(b"a/b")
    + file_entry(b"a/b/c", b"deep")
    + file_entry(b"a/d", b"")
    + file_entry(b"e", LARGE_FILE)
    + end_entry(5, 4 + len(LARGE_FILE))
)
REFUSED_UPLOADS = {
    "a path rule": directory_entry(b"..") + end_entry(1, 0),
    "an order fault": file_entry(b"b", b"") + file_entry(b"a", b"") + end_entry(2, 0),
    "an END mismatch": file_entry(b"a", b"x") + end_entry(1, 2),
    "a top-level node_modules": directory_entry(b"node_modules") + end_entry(1, 0),
}
CAPPED_UPLOADS = {
    "a file over the megabytes": file_header(b"f", MEGABYTE + 1),
    "an entry after the maximum": entries_at_the_maximum() + bytes([RecordTag.FILE]),
}


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv("PATH", PATH)
    previous = os.umask(CONTROLLER_UMASK)
    try:
        yield tmp_path
    finally:
        os.umask(previous)


def modes_below(deck: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(deck)): (
            stat.S_IMODE(path.stat().st_mode),
            path.stat().st_uid,
        )
        for path in deck.rglob("*")
    }


def test_a_nested_upload_through_the_real_roles_is_stored_0755_and_0644(
    sandbox: Path,
) -> None:
    deck = sandbox / "deck"

    life = run_life(
        support.VALID_STEP + NESTED_UPLOAD,
        ChainSpawn(programs={"send": [EXITING_SENT]}),
        LifeScenario(work=sandbox),
    )

    assert life.life_end == LifeEnd.CONSUMED
    assert modes_below(deck) == {
        "node_modules": (STEP_SCRATCH_MODE, OWN_UID),
        "a": (DIRECTORY_MODE, OWN_UID),
        "a/b": (DIRECTORY_MODE, OWN_UID),
        "a/b/c": (FILE_MODE, OWN_UID),
        "a/d": (FILE_MODE, OWN_UID),
        "e": (FILE_MODE, OWN_UID),
    }
    assert (deck / "e").read_bytes() == LARGE_FILE
    assert stat.S_IMODE(deck.stat().st_mode) == support.DECK_CLOSED_MODE


def spawn_controller(volume: Path, log: Path, work: Path) -> GatedChild:
    return spawn_gated(
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
        environment={"PATH": PATH},
        pass_fds=(),
    )


@pytest.mark.parametrize(
    "refused", REFUSED_UPLOADS.values(), ids=REFUSED_UPLOADS.keys()
)
def test_a_refused_upload_over_a_controller_closes_with_no_reply_and_spawns_no_step(
    volume: Path, tmp_path: Path, refused: bytes
) -> None:
    assert_refused_upload_closes(volume, tmp_path, refused, read_to_end_or_reset)


@pytest.mark.parametrize("capped", CAPPED_UPLOADS.values(), ids=CAPPED_UPLOADS.keys())
def test_a_capped_upload_over_a_controller_gets_status_then_eof_and_spawns_no_step(
    volume: Path, tmp_path: Path, capped: bytes
) -> None:
    assert_refused_upload_closes(volume, tmp_path, capped, read_to_end)


def assert_refused_upload_closes(
    volume: Path,
    tmp_path: Path,
    refused: bytes,
    read_reply: Callable[[socket.socket], bytes],
) -> None:
    log = tmp_path / "controller.log"
    log.touch()
    controller = spawn_controller(volume, log, tmp_path)
    try:
        controller.release()
        with closing(
            support.connect_when_serving(volume, controller, WAIT_SECONDS)
        ) as client:
            client.settimeout(support.BULK_SEND_SECONDS)
            client.sendall(support.VALID_STEP + refused)
            client.settimeout(WAIT_SECONDS)
            reply = read_reply(client)
        exit_status = controller.wait_within(WAIT_SECONDS)
    finally:
        lines = end_controller(controller, log)

    assert reply == support.STATUS
    assert exit_status == 0
    assert [line.split()[1] for line in lines] == ["idle", "upload"]


def test_a_nested_upload_over_a_controller_gets_status_built_talk_entries_end_and_eof(
    volume: Path, tmp_path: Path
) -> None:
    life = controller_life(volume, tmp_path, support.VALID_STEP + NESTED_UPLOAD)

    assert life.reply == (
        support.STATUS
        + support.OUTCOME_BUILT
        + support.talk_entries(step_support.TALK_INDEX)
    )
    assert life.exit_status == 0
    assert life.roles == ["idle", "upload", "step", "send"]
    assert life.announced_pid == life.step_pid
    assert not life.present_after_exit
