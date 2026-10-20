"""An END-only upload through the real roles, and the deck around it."""

import errno
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import tests.test_worker_record_chain as record_chain
from presentator.host import worker, worker_serving
from presentator.host.worker_serving import LifeEnd
from tests.test_worker_record_chain import (
    END,
    HELLO,
    PATH,
    STATUS,
    VALID_STEP,
    WAIT_SECONDS,
    ChainSpawn,
    LifeScenario,
    run_life,
)
from tests.worker_support import DECK_CLOSED_MODE, DECK_OPEN_MODE, PRIVATE_MODE

DECK_MODE_REPORTER = """
import os, stat, sys
mode = stat.S_IMODE(os.stat(sys.argv[3]).st_mode)
os.write(int(sys.argv[1]), str(oct(mode)).encode())
sys.exit(6)
"""
EXITING_SEVEN = "import sys; sys.exit(7)"
CONTROLLER_UMASK = 0o077
HOLD_SECONDS = WAIT_SECONDS
JOIN_SECONDS = 0.5


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv("PATH", PATH)
    previous = os.umask(CONTROLLER_UMASK)
    try:
        yield tmp_path
    finally:
        os.umask(previous)


def on_deck(sandbox: Path) -> LifeScenario:
    return LifeScenario(deck=sandbox / "deck")


def deck_mode(sandbox: Path) -> int:
    return stat.S_IMODE((sandbox / "deck").stat().st_mode)


def read_status(client: socket.socket) -> None:
    received = b""
    while len(received) < len(STATUS):
        received += client.recv(len(STATUS) - len(received))
    assert received == STATUS


def test_an_end_only_upload_is_answered_with_status_then_ends_the_life_step_accepted(
    sandbox: Path,
) -> None:
    modes_during_upload: list[int] = []

    def status_then_end(client: socket.socket) -> None:
        read_status(client)
        modes_during_upload.append(deck_mode(sandbox))
        client.sendall(END)

    life = run_life(VALID_STEP, ChainSpawn(), on_deck(sandbox), status_then_end)

    assert life.reply == b""
    assert life.life_end == LifeEnd.STEP_ACCEPTED
    assert life.spawn.roles == ["idle", "upload"]
    assert modes_during_upload == [DECK_OPEN_MODE]
    assert deck_mode(sandbox) == DECK_CLOSED_MODE


def test_the_deck_a_fake_upload_child_sees_is_1777_and_0755_after_it_exits(
    sandbox: Path,
) -> None:
    deck = sandbox / "deck"
    spawn = ChainSpawn(programs={"upload": [DECK_MODE_REPORTER, str(deck)]})

    life = run_life(VALID_STEP, spawn, LifeScenario(deck=deck))

    assert life.reply == str(oct(DECK_OPEN_MODE)).encode()
    assert life.life_end == LifeEnd.STEP_ACCEPTED
    assert deck_mode(sandbox) == DECK_CLOSED_MODE


class ConversationFailedError(Exception):
    """The client side of a test conversation broke."""


class CleanUpFailedError(OSError):
    """Ending the children of a test life broke."""


class GatedClosingDeck(type(Path())):
    """A deck whose closing waits until the test lets the controller go on."""

    released: threading.Event

    def chmod(self, mode: int, *, follow_symlinks: bool = True) -> None:
        if mode == DECK_CLOSED_MODE:
            self.released.wait(HOLD_SECONDS)
        super().chmod(mode, follow_symlinks=follow_symlinks)


class ReleasingSpawn(ChainSpawn):
    """Lets the gated deck close once the children are reaped, then fails if asked."""

    def __init__(self, released: threading.Event, failure: OSError | None) -> None:
        """Gate on the given event and fail the reaping with the given error."""
        super().__init__()
        self.released = released
        self.failure = failure

    def kill_and_reap(self) -> None:
        super().kill_and_reap()
        self.released.set()
        if self.failure is not None:
            raise self.failure


CLEAN_UP_FAILURES = {
    "the clean-up succeeds": None,
    "the clean-up fails": CleanUpFailedError("reap failed"),
}


@pytest.mark.parametrize(
    "failure", CLEAN_UP_FAILURES.values(), ids=CLEAN_UP_FAILURES.keys()
)
def test_a_failing_conversation_stays_primary_and_returns_only_after_the_deck_closed(
    sandbox: Path, failure: OSError | None
) -> None:
    def fail_after_status(client: socket.socket) -> None:
        read_status(client)
        raise ConversationFailedError

    deck = GatedClosingDeck(sandbox / "deck")
    deck.released = threading.Event()

    with pytest.raises(ConversationFailedError) as raised:
        run_life(
            VALID_STEP,
            ReleasingSpawn(deck.released, failure),
            LifeScenario(deck=deck),
            fail_after_status,
        )

    assert deck_mode(sandbox) == DECK_CLOSED_MODE
    notes = "".join(getattr(raised.value, "__notes__", []))
    assert (failure is None) == (not notes)
    assert failure is None or repr(failure) in notes


class StuckController:
    """A seal that holds the controller thread until the test lets it go."""

    def __init__(self) -> None:
        """Start with the seal not yet entered and not yet released."""
        self.entered = threading.Event()
        self.released = threading.Event()

    def seal(self, record: int) -> None:
        self.entered.set()
        self.released.wait(HOLD_SECONDS)
        worker.seal_record(record)

    def await_stuck(self, _client: socket.socket) -> None:
        assert self.entered.wait(HOLD_SECONDS)


@pytest.mark.parametrize("expected", [AssertionError, ConversationFailedError])
def test_a_controller_that_outlives_the_join_fails_loudly_on_every_path(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected: type[Exception],
) -> None:
    monkeypatch.setattr(record_chain, "WAIT_SECONDS", JOIN_SECONDS)
    stuck = StuckController()

    def converse(client: socket.socket) -> None:
        stuck.await_stuck(client)
        if expected is ConversationFailedError:
            raise ConversationFailedError

    try:
        with pytest.raises(expected) as raised:
            run_life(
                VALID_STEP,
                ChainSpawn(),
                LifeScenario(deck=sandbox / "deck", seal=stuck.seal),
                converse,
            )
    finally:
        stuck.released.set()

    notes = "".join(getattr(raised.value, "__notes__", []))
    assert ("AssertionError" in notes) == (expected is ConversationFailedError)


def step_message(header: bytes) -> bytes:
    return b"PBW\x01\x02" + len(header).to_bytes(2, "big") + header


REJECTED_HEADERS = {
    "bad magic": b"X" + VALID_STEP[1:],
    "bad version": VALID_STEP[:3] + b"\x09" + VALID_STEP[4:],
    "unknown kind": VALID_STEP[:4] + b"\x09" + VALID_STEP[5:],
    "too short a length": step_message(b"{"),
    "too long a length": VALID_STEP[:5] + b"\x10\x01",
    "not JSON": step_message(b"nope"),
    "unknown step": step_message(b'{"step":"x","slug":"my-deck"}'),
    "slug ..": step_message(b'{"step":"build","slug":".."}'),
    "a HELLO": HELLO,
}


@pytest.mark.parametrize("sent", REJECTED_HEADERS.values(), ids=REJECTED_HEADERS.keys())
def test_a_rejected_or_answered_header_never_makes_the_deck(
    sandbox: Path, sent: bytes
) -> None:
    life = run_life(sent, ChainSpawn(), on_deck(sandbox))

    assert life.spawn.roles == ["idle"]
    assert not (sandbox / "deck").exists()


def test_an_idle_stall_never_makes_the_deck(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_serving, "HEADER_SECONDS", 0.5)

    life = run_life(b"", ChainSpawn(), on_deck(sandbox))

    assert life.life_end is None
    assert life.spawn.roles == ["idle"]
    assert not (sandbox / "deck").exists()


def test_a_failing_seal_never_makes_the_deck(sandbox: Path) -> None:
    def refuse(_record: int) -> None:
        raise OSError(errno.EBUSY)

    life = run_life(
        VALID_STEP, ChainSpawn(), LifeScenario(deck=sandbox / "deck", seal=refuse)
    )

    assert life.life_end == LifeEnd.SEAL_FAILED
    assert not (sandbox / "deck").exists()


def hang_up(client: socket.socket) -> None:
    read_status(client)
    client.shutdown(socket.SHUT_RDWR)


def half_close(client: socket.socket) -> None:
    read_status(client)
    client.shutdown(socket.SHUT_WR)


def send_after_status(sent: bytes) -> Callable[[socket.socket], None]:
    def converse(client: socket.socket) -> None:
        read_status(client)
        client.sendall(sent)

    return converse


def drip_past_the_budget(client: socket.socket) -> None:
    read_status(client)
    client.sendall(END[:1])


UPLOAD_FAILURES: dict[
    str, tuple[Callable[[socket.socket], None], str | None, LifeEnd]
] = {
    "a hang-up": (hang_up, None, LifeEnd.WIRE_EXIT),
    "a half-close before END": (half_close, None, LifeEnd.WIRE_EXIT),
    "a non-zero END count": (
        send_after_status(b"\x03\x00\x00\x00\x01" + bytes(8)),
        None,
        LifeEnd.WIRE_EXIT,
    ),
    "an unknown tag": (
        send_after_status(b"\x09" + bytes(12)),
        None,
        LifeEnd.WIRE_EXIT,
    ),
    "a drip past U": (drip_past_the_budget, None, LifeEnd.UPLOAD_EXPIRED),
    "an exit other than uploaded": (
        lambda _client: None,
        EXITING_SEVEN,
        LifeEnd.WIRE_EXIT,
    ),
}


@pytest.mark.parametrize(
    ("converse", "program", "life_end"),
    UPLOAD_FAILURES.values(),
    ids=UPLOAD_FAILURES.keys(),
)
def test_a_failed_upload_leaves_the_deck_0755_closes_without_reply_and_ends_the_life(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    converse: Callable[[socket.socket], None],
    program: str | None,
    life_end: LifeEnd,
) -> None:
    monkeypatch.setattr(worker_serving, "UPLOAD_SECONDS", 1.5)
    spawn = ChainSpawn(programs={} if program is None else {"upload": [program]})

    started = time.monotonic()
    life = run_life(VALID_STEP, spawn, on_deck(sandbox), converse)

    assert life.life_end == life_end
    assert life.reply == b""
    assert life.spawn.roles == ["idle", "upload"]
    assert deck_mode(sandbox) == DECK_CLOSED_MODE
    assert time.monotonic() - started < 1.5 + WAIT_SECONDS


def test_an_existing_deck_ends_the_life_before_any_upload_spawn(
    sandbox: Path,
) -> None:
    (sandbox / "deck").mkdir(mode=PRIVATE_MODE)

    life = run_life(VALID_STEP, ChainSpawn(), on_deck(sandbox))

    assert life.life_end == LifeEnd.DECK_FAILED
    assert life.reply == b""
    assert life.spawn.roles == ["idle"]
    assert deck_mode(sandbox) == PRIVATE_MODE
