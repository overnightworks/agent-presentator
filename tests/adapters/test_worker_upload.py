"""The deck's two effects, and how the controller runs them."""

import errno
import os
import socket
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from presentator.host import worker
from presentator.host.worker_serving import LifeEnd, hand_over
from presentator.host.worker_wire import WireExit
from tests.worker_support import (
    ACCEPT_TIME,
    DECK_CLOSED_MODE,
    DECK_OPEN_MODE,
    PRIVATE_MODE,
    ScriptedLife,
)

CONTROLLER_UMASK = 0o077


@pytest.fixture
def deck_path(tmp_path: Path) -> Iterator[Path]:
    previous = os.umask(CONTROLLER_UMASK)
    try:
        yield tmp_path / "deck"
    finally:
        os.umask(previous)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def accepted_life(**changes: object) -> ScriptedLife:
    return replace(ScriptedLife(exit_status=WireExit.ACCEPTED), **changes)


def test_the_deck_is_world_writable_and_sticky_despite_the_controllers_umask(
    deck_path: Path,
) -> None:
    worker.open_deck(deck_path)

    assert mode_of(deck_path) == DECK_OPEN_MODE


def test_closing_the_deck_takes_it_back_to_0755(deck_path: Path) -> None:
    worker.open_deck(deck_path)

    worker.close_deck(deck_path)

    assert mode_of(deck_path) == DECK_CLOSED_MODE


def test_an_existing_deck_is_never_reused_nor_changed(deck_path: Path) -> None:
    deck_path.mkdir(mode=PRIVATE_MODE)
    (deck_path / "leftover").write_text("kept")

    with pytest.raises(FileExistsError):
        worker.open_deck(deck_path)

    assert mode_of(deck_path) == PRIVATE_MODE
    assert (deck_path / "leftover").read_text() == "kept"


def test_the_deck_is_1777_until_the_upload_run_ends_and_0755_afterwards(
    connection: socket.socket, listener: socket.socket, deck_path: Path
) -> None:
    life = accepted_life()
    modes_when_closing: list[int] = []

    def close_noting_mode(closed: Path) -> None:
        modes_when_closing.append(mode_of(closed))
        worker.close_deck(closed)

    serving = replace(
        life.serving(),
        deck=deck_path,
        open_deck=worker.open_deck,
        close_deck=close_noting_mode,
    )

    assert (
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)
        == LifeEnd.CONSUMED
    )
    assert modes_when_closing == [DECK_OPEN_MODE]
    assert mode_of(deck_path) == DECK_CLOSED_MODE


def test_the_deck_is_opened_after_the_seal_and_closed_after_the_upload_waited(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life()

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.CONSUMED
    names = [event.split()[0] for event in life.events]
    opened = names.index("open-deck")
    assert names[opened - 1] == "seal"
    assert names[opened + 1] == "spawn"
    assert names.index("close-deck") > names.index("wait", opened)


DECK_FAILURES = {
    "an unmakeable directory": OSError(errno.EACCES, "no mkdir"),
    "an existing directory": FileExistsError(errno.EEXIST, "exists"),
}


@pytest.mark.parametrize("failure", DECK_FAILURES.values(), ids=DECK_FAILURES.keys())
def test_a_deck_that_cannot_be_opened_ends_the_life_before_any_upload_spawn(
    connection: socket.socket, listener: socket.socket, failure: OSError
) -> None:
    life = accepted_life(deck_open_error=failure)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.DECK_FAILED
    assert len(life.spawned) == 1
    assert "close-deck" not in life.events


def test_a_failing_seal_never_opens_the_deck(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life(seal_error=OSError("no seal"))

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.SEAL_FAILED
    assert "open-deck" not in life.events


def test_a_rejected_header_never_opens_the_deck(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = ScriptedLife(exit_status=WireExit.CLOSED)

    assert hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener) is None
    assert "open-deck" not in life.events


UPLOAD_ENDS = {
    "an expiry": ({"upload_stalled": True}, LifeEnd.UPLOAD_EXPIRED),
    "a wire exit": ({"upload_exit_status": WireExit.CLOSED}, LifeEnd.WIRE_EXIT),
    "a gate mismatch": ({"upload_gate_admits": False}, LifeEnd.GATE),
    "a spawn failure": ({"upload_spawn_error": OSError("no")}, LifeEnd.SPAWN),
}


@pytest.mark.parametrize(
    ("changes", "life_end"), UPLOAD_ENDS.values(), ids=UPLOAD_ENDS.keys()
)
def test_the_deck_is_closed_after_every_way_the_upload_run_ends(
    connection: socket.socket,
    listener: socket.socket,
    changes: dict[str, object],
    life_end: LifeEnd,
) -> None:
    life = accepted_life(**changes)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == life_end
    assert life.events.count("open-deck") == life.events.count("close-deck") == 1


def test_the_deck_is_closed_when_the_upload_run_raises_and_the_original_propagates(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life(wait_error=OSError("lost"), wait_error_at=1)

    serving = life.serving()

    with pytest.raises(OSError, match="lost") as raised:
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)

    assert life.events.index("close-deck") > life.events.index("kill")
    assert not getattr(raised.value, "__notes__", [])


def test_a_deck_that_cannot_be_closed_is_not_swallowed_after_the_original_is_noted(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life(
        wait_error=OSError("lost"),
        wait_error_at=1,
        deck_close_error=OSError("no chmod"),
    )

    serving = life.serving()

    with pytest.raises(OSError, match="lost") as raised:
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)

    assert any("no chmod" in note for note in raised.value.__notes__)


def test_a_deck_that_cannot_be_closed_after_a_clean_run_raises(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life(deck_close_error=OSError("no chmod"))

    serving = life.serving()

    with pytest.raises(OSError, match="no chmod"):
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)
