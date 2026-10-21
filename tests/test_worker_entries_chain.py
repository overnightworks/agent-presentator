"""DIR and FILE entries through the real roles: the upload child writes the deck."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from presentator.adapters.build_wire import MEGABYTE, RecordTag
from presentator.adapters.worker_children import GatedChild, spawn_gated
from presentator.host.worker_serving import LifeEnd
from tests.test_worker_record_chain import (
    EXITING_SENT,
    OUTCOME_FAILED,
    OWN_IDENTITY,
    PATH,
    STATUS,
    VALID_STEP,
    WAIT_SECONDS,
    ChainSpawn,
    LifeScenario,
    connect_when_serving,
    end_controller,
    read_to_end,
    read_to_end_or_reset,
    run_life,
)
from tests.test_worker_reply_chain import CONSUMING_CONTROLLER
from tests.worker_support import (
    DECK_CLOSED_MODE,
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
        VALID_STEP + NESTED_UPLOAD,
        ChainSpawn(programs={"send": [EXITING_SENT]}),
        LifeScenario(deck=deck),
    )

    assert life.life_end == LifeEnd.CONSUMED
    assert modes_below(deck) == {
        "a": (DIRECTORY_MODE, OWN_UID),
        "a/b": (DIRECTORY_MODE, OWN_UID),
        "a/b/c": (FILE_MODE, OWN_UID),
        "a/d": (FILE_MODE, OWN_UID),
        "e": (FILE_MODE, OWN_UID),
    }
    assert (deck / "e").read_bytes() == LARGE_FILE
    assert stat.S_IMODE(deck.stat().st_mode) == DECK_CLOSED_MODE


def spawn_controller(volume: Path, log: Path, deck: Path) -> GatedChild:
    return spawn_gated(
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


@pytest.mark.parametrize(
    "refused", REFUSED_UPLOADS.values(), ids=REFUSED_UPLOADS.keys()
)
def test_a_refused_upload_over_a_controller_closes_with_no_reply_and_spawns_no_step(
    volume: Path, tmp_path: Path, refused: bytes
) -> None:
    log = tmp_path / "controller.log"
    log.touch()
    controller = spawn_controller(volume, log, tmp_path / "deck")
    try:
        controller.release()
        with closing(connect_when_serving(volume, controller)) as client:
            client.sendall(VALID_STEP + refused)
            reply = read_to_end_or_reset(client)
        exit_status = controller.wait_within(WAIT_SECONDS)
    finally:
        lines = end_controller(controller, log)

    assert reply == STATUS
    assert exit_status == 0
    assert [line.split()[1] for line in lines] == ["idle", "upload"]


def test_a_nested_upload_over_a_controller_gets_status_failed_end_and_eof(
    volume: Path, tmp_path: Path
) -> None:
    log = tmp_path / "controller.log"
    log.touch()
    controller = spawn_controller(volume, log, tmp_path / "deck")
    try:
        controller.release()
        with closing(connect_when_serving(volume, controller)) as client:
            client.sendall(VALID_STEP + NESTED_UPLOAD)
            reply = read_to_end(client)
        exit_status = controller.wait_within(WAIT_SECONDS)
    finally:
        lines = end_controller(controller, log)

    assert reply == STATUS + OUTCOME_FAILED + end_entry(0, 0)
    assert exit_status == 0
    assert [line.split()[1] for line in lines] == ["idle", "upload", "send"]
