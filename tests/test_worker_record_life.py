"""The controller's handling of the request record across an accepted step."""

import errno
import mmap
import os
import socket
from collections.abc import Callable
from dataclasses import replace

import pytest

from presentator.host import worker
from presentator.host.worker_serving import (
    UPLOAD_SECONDS,
    LifeEnd,
    hand_over,
)
from presentator.host.worker_wire import WireExit
from tests.worker_support import ACCEPT_TIME, RecordingCreation, ScriptedLife

ACCEPTED_ARM = "arm 447.0"


def accepted_life(**changes: object) -> ScriptedLife:
    return replace(ScriptedLife(exit_status=WireExit.ACCEPTED), **changes)


def test_the_watchdog_is_re_armed_before_the_record_is_sealed_and_passed_unread(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life()

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.CONSUMED
    rearmed = life.events.index(ACCEPTED_ARM)
    assert life.events[rearmed : rearmed + 2] == [ACCEPTED_ARM, "seal"]
    idle, upload, sender = life.spawned
    assert idle.pass_fds == upload.pass_fds == sender.pass_fds
    assert life.sealed_descriptors == [idle.pass_fds[1]]


UPLOAD_WAIT = 1

CONSUMERS = {
    "the re-arm": {"arm_takes": 0.3},
    "the seal": {"seal_takes": 0.4},
    "the upload child's spawn": {"upload_spawn_takes": 0.35},
    "the upload child's gate check": {"inspection_takes": 0.5},
    "the upload child's release": {"release_takes": 0.25},
}


@pytest.mark.parametrize("consumed", CONSUMERS.values(), ids=CONSUMERS.keys())
def test_the_upload_wait_is_what_remains_of_the_budget_taken_at_the_accepted_exit(
    connection: socket.socket, listener: socket.socket, consumed: dict[str, float]
) -> None:
    life = accepted_life(**consumed)

    hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    [taken] = consumed.values()
    assert life.budgets_waited[UPLOAD_WAIT] == pytest.approx(UPLOAD_SECONDS - taken)


def test_an_upload_budget_spent_before_the_release_prevents_the_release(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life(seal_takes=UPLOAD_SECONDS + 1, upload_stalled=True)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.UPLOAD_EXPIRED
    upload_leg = life.events[life.events.index("seal") :]
    assert upload_leg == [
        "seal",
        "open-deck",
        "spawn",
        "status",
        "kill",
        "wait",
        "close",
        "close-deck",
    ]


def test_an_upload_budget_spent_during_the_release_is_not_waited_for(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life(
        release_takes=0.2, seal_takes=UPLOAD_SECONDS - 0.1, upload_stalled=True
    )

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.UPLOAD_EXPIRED
    upload_leg = life.events[life.events.index("seal") :]
    assert upload_leg == [
        "seal",
        "open-deck",
        "spawn",
        "status",
        "release",
        "kill",
        "wait",
        "close",
        "close-deck",
    ]
    assert len(life.budgets_waited) == 1


def test_a_stalled_upload_child_is_waited_for_its_remaining_time_then_killed(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life(upload_stalled=True)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.UPLOAD_EXPIRED
    assert life.budgets_waited[-1] == UPLOAD_SECONDS
    assert life.events[-4:] == ["kill", "wait", "close", "close-deck"]


def test_a_writable_shared_mapping_makes_the_production_seal_fail_before_any_upload(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    mappings: list[mmap.mmap] = []
    refusals: list[OSError] = []

    def seal_while_mapped(record: int) -> None:
        os.ftruncate(record, mmap.PAGESIZE)
        mappings.append(
            mmap.mmap(record, mmap.PAGESIZE, mmap.MAP_SHARED, mmap.PROT_WRITE)
        )
        try:
            worker.seal_record(record)
        except OSError as refused:
            refusals.append(refused)
            raise

    life = accepted_life()
    try:
        outcome = hand_over(
            connection,
            replace(life.serving(), seal=seal_while_mapped),
            ACCEPT_TIME,
            listener=listener,
        )
    finally:
        for mapping in mappings:
            mapping.close()

    [refused] = refusals
    assert refused.errno == errno.EBUSY
    assert outcome == LifeEnd.SEAL_FAILED
    assert len(life.spawned) == 1


CONTROLLER_READS: dict[str, Callable[[int], object]] = {
    "read": lambda record: os.read(record, 1),
    "pread": lambda record: os.pread(record, 1, 0),
    "readv": lambda record: os.readv(record, [bytearray(1)]),
    "preadv": lambda record: os.preadv(record, [bytearray(1)], 0),
}


def forbid_reads_of(
    monkeypatch: pytest.MonkeyPatch, created: RecordingCreation
) -> None:
    for name in CONTROLLER_READS:
        genuine = getattr(os, name)

        def guarded(
            descriptor: int,
            *arguments: object,
            genuine: Callable[..., object] = genuine,
        ) -> object:
            assert descriptor != created.descriptor, "the controller read the record"
            return genuine(descriptor, *arguments)

        monkeypatch.setattr(os, name, guarded)


def test_the_controller_reads_no_byte_of_the_record_over_a_whole_accepted_life(
    connection: socket.socket,
    listener: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    life = accepted_life()
    forbid_reads_of(monkeypatch, life.creation)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)

    assert outcome == LifeEnd.CONSUMED


@pytest.mark.parametrize("read", CONTROLLER_READS.values(), ids=CONTROLLER_READS.keys())
def test_a_controller_that_reads_the_record_by_any_call_fails_the_interception(
    connection: socket.socket,
    listener: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[[int], object],
) -> None:
    life = accepted_life()
    forbid_reads_of(monkeypatch, life.creation)
    serving = replace(life.serving(), seal=read)

    with pytest.raises(AssertionError, match="read the record"):
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)


class ArmFails:
    """A watchdog whose re-arm raises."""

    def arm(self, remaining: float) -> None:
        raise RuntimeError(remaining)

    def cancel(self) -> None:
        pass


PATHS = {
    "a spawn failure": (lambda: ScriptedLife(spawn_error=OSError("no")), LifeEnd.SPAWN),
    "a gate mismatch": (lambda: ScriptedLife(gate_admits=False), LifeEnd.GATE),
    "a header expiry": (lambda: ScriptedLife(headers_stalled=1), None),
    "a HELLO answered": (lambda: ScriptedLife(exit_status=WireExit.ANSWERED), None),
    "a rejection": (lambda: ScriptedLife(exit_status=WireExit.CLOSED), None),
    "a wire exit": (lambda: ScriptedLife(exit_status=1), LifeEnd.WIRE_EXIT),
    "a failing seal": (
        lambda: accepted_life(seal_error=OSError("no seal")),
        LifeEnd.SEAL_FAILED,
    ),
    "an upload spawn failure": (
        lambda: accepted_life(upload_spawn_error=OSError("no")),
        LifeEnd.SPAWN,
    ),
    "an upload gate mismatch": (
        lambda: accepted_life(upload_gate_admits=False),
        LifeEnd.GATE,
    ),
    "an upload expiry": (
        lambda: accepted_life(upload_stalled=True),
        LifeEnd.UPLOAD_EXPIRED,
    ),
    "an upload wire exit": (
        lambda: accepted_life(upload_exit_status=WireExit.CLOSED),
        LifeEnd.WIRE_EXIT,
    ),
    "a sender spawn failure": (
        lambda: accepted_life(send_spawn_error=OSError("no")),
        LifeEnd.SPAWN,
    ),
    "a sender gate mismatch": (
        lambda: accepted_life(send_gate_admits=False),
        LifeEnd.GATE,
    ),
    "a sender expiry": (
        lambda: accepted_life(send_stalled=True),
        LifeEnd.REPLY_EXPIRED,
    ),
    "a sender wire exit": (
        lambda: accepted_life(send_exit_status=WireExit.CLOSED),
        LifeEnd.WIRE_EXIT,
    ),
    "a success": (accepted_life, LifeEnd.CONSUMED),
}


@pytest.mark.parametrize(("make_life", "life_end"), PATHS.values(), ids=PATHS.keys())
def test_the_controllers_record_descriptor_is_closed_on_every_path(
    connection: socket.socket,
    listener: socket.socket,
    make_life: Callable[[], ScriptedLife],
    life_end: LifeEnd | None,
) -> None:
    life = make_life()

    assert (
        hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)
        == life_end
    )

    assert life.creation.is_closed()


def test_the_controllers_record_descriptor_is_closed_when_the_re_arm_raises(
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    life = accepted_life()
    serving = replace(life.serving(), watchdog=ArmFails())

    with pytest.raises(RuntimeError):
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)

    assert life.creation.is_closed()


@pytest.mark.parametrize("role_index", [0, 1, 2], ids=["idle", "upload", "send"])
@pytest.mark.parametrize(
    ("cleanup_fault", "cleanup_failure"),
    [
        ({}, None),
        ({"kill_error": OSError("kill failed")}, "kill failed"),
        ({"reap_error": KeyboardInterrupt("reap failed")}, "reap failed"),
    ],
    ids=["clean", "kill-fails", "reap-fails"],
)
def test_a_child_is_killed_and_reaped_and_the_original_error_survives_a_failing_cleanup(
    connection: socket.socket,
    listener: socket.socket,
    role_index: int,
    cleanup_fault: dict[str, BaseException],
    cleanup_failure: str | None,
) -> None:
    life = accepted_life(
        wait_error=OSError("wait failed"), wait_error_at=role_index, **cleanup_fault
    )
    serving = life.serving()

    with pytest.raises(OSError, match="wait failed") as raised:
        hand_over(connection, serving, ACCEPT_TIME, listener=listener)

    expected = ["kill", "wait", "close", *["close-deck"] * (role_index == 1)]
    assert life.events[-len(expected) :] == expected
    assert life.events.count("close-deck") == min(role_index, 1)
    notes = getattr(raised.value, "__notes__", [])
    assert len(notes) == (cleanup_failure is not None)
    assert all(cleanup_failure in note for note in notes)
