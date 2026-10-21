"""The step phase on a scripted life: its gate, its deadlines and its cancellation."""

import socket
import subprocess
import sys
from contextlib import closing
from dataclasses import replace

import pytest

from presentator.host.worker_serving import (
    REPLY_SECONDS,
    LifeEnd,
    hand_over,
)
from presentator.host.worker_wire import WireExit
from tests.worker_support import (
    ACCEPT_TIME,
    STEP_ENVIRONMENT,
    STEP_EXIT_DESCRIPTOR,
    STEP_PROJECT,
    ScriptedLife,
    ScriptedStep,
    open_descriptor_count,
    sender_leg,
    step_leg,
)

BUILD_SECONDS = 300.0
WIRE_SPAWNS = 3
FIRST_STEP_WAIT = 2
KILLED_UNRELEASED = ["step kill", "step wait", "step close"]


def accepted_life(**changes: object) -> ScriptedLife:
    life = ScriptedLife(
        exit_status=WireExit.ACCEPTED, build_timeout_seconds=BUILD_SECONDS
    )
    return replace(life, **changes)


def run(
    life: ScriptedLife, connection: socket.socket, listener: socket.socket
) -> LifeEnd | None:
    return hand_over(connection, life.serving(), ACCEPT_TIME, listener=listener)


def test_a_step_that_exits_zero_runs_between_the_deck_and_the_sender(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life()

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CONSUMED
    assert step_leg(life) == [
        "prepare",
        "step spawn",
        "step status",
        "step release",
        "step readable",
        "step wait",
        "step close",
    ]
    assert life.events.index("close-deck") < life.events.index("prepare")


def test_a_step_whose_gate_refuses_is_killed_before_go_and_the_sender_still_answers(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life(step=ScriptedStep(gate_admits=False))

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CONSUMED
    assert step_leg(life) == [
        "prepare",
        "step spawn",
        "step status",
        *KILLED_UNRELEASED,
    ]
    [step_spawned] = life.step.spawned
    idle, _upload, sender = life.spawned
    assert step_spawned.pass_fds == (sender.pass_fds[1],) == (idle.pass_fds[1],)


STEP_FAILURES = {
    "a prepare failure": (ScriptedStep(prepare_error=OSError("exists")), ["prepare"]),
    "a spawn failure": (
        ScriptedStep(spawn_error=OSError("no")),
        ["prepare", "step spawn"],
    ),
    "an unreadable status": (
        ScriptedStep(status=OSError("gone")),
        ["prepare", "step spawn", "step status", *KILLED_UNRELEASED],
    ),
    "a broken pipe at the release": (
        ScriptedStep(release_error=BrokenPipeError()),
        ["prepare", "step spawn", "step status", "step release", *KILLED_UNRELEASED],
    ),
}


@pytest.mark.parametrize(
    ("step", "leg"), STEP_FAILURES.values(), ids=STEP_FAILURES.keys()
)
def test_a_step_that_cannot_run_is_never_released_and_the_sender_still_answers(
    connection: socket.socket,
    listener: socket.socket,
    step: ScriptedStep,
    leg: list[str],
) -> None:
    life = accepted_life(step=step)

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CONSUMED
    assert step_leg(life) == leg
    assert len(life.spawned) == WIRE_SPAWNS


GATE_TIMES = {
    "the end of the gate budget": (2.0, KILLED_UNRELEASED),
    "just before it": (
        1.9,
        ["step release", "step readable", "step wait", "step close"],
    ),
}


@pytest.mark.parametrize(
    ("spawn_takes", "after_status"), GATE_TIMES.values(), ids=GATE_TIMES.keys()
)
def test_the_gate_budget_covers_the_paths_the_spawn_and_the_inspection(
    connection: socket.socket,
    listener: socket.socket,
    spawn_takes: float,
    after_status: list[str],
) -> None:
    step = ScriptedStep(
        prepare_takes=2.0, spawn_takes=spawn_takes, inspection_takes=1.0
    )
    life = accepted_life(step=step)

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CONSUMED
    assert step_leg(life)[3:] == after_status
    assert len(life.spawned) == WIRE_SPAWNS


def test_the_step_is_spawned_as_the_launcher_with_the_record_and_its_own_streams(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life()
    descriptors_before = open_descriptor_count()

    run(life, connection, listener)

    [spawned] = life.step.spawned
    record = life.spawned[0].pass_fds[1]
    [stderr] = life.prepared_descriptors
    assert spawned.command == [
        sys.executable,
        "-I",
        "-m",
        "presentator.host.worker_step",
        "--record-fd",
        str(record),
        "--work",
        str(life.work),
    ]
    assert tuple(spawned.pass_fds) == (record,)
    assert spawned.environment == STEP_ENVIRONMENT
    assert spawned.surroundings.cwd == STEP_PROJECT
    assert spawned.surroundings.stdout == subprocess.DEVNULL
    assert spawned.surroundings.stderr == stderr
    assert open_descriptor_count() == descriptors_before


def test_the_controller_closes_the_prepared_stderr_when_the_spawn_fails(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life(step=ScriptedStep(spawn_error=OSError("no")))
    descriptors_before = open_descriptor_count()

    run(life, connection, listener)

    assert len(life.prepared_descriptors) == 1
    assert open_descriptor_count() == descriptors_before


def test_the_step_wait_is_what_remains_of_the_build_bound_and_watches_three_things(
    connection: socket.socket, listener: socket.socket
) -> None:
    step = ScriptedStep(wake_takes=1.0, listener_wakes=2)
    life = accepted_life(step=step)

    run(life, connection, listener)

    assert life.budgets_waited[FIRST_STEP_WAIT : FIRST_STEP_WAIT + 3] == [
        BUILD_SECONDS,
        BUILD_SECONDS - 1,
        BUILD_SECONDS - 2,
    ]
    assert step.watched[0] == {
        STEP_EXIT_DESCRIPTOR,
        listener.fileno(),
        life.spawned[0].pass_fds[0],
    }


def test_a_step_that_stalls_past_the_build_bound_is_killed_at_its_end(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life(step=ScriptedStep(stalled=True))

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CONSUMED
    assert step_leg(life)[3:] == [
        "step release",
        "step readable",
        "step kill",
        "step wait",
        "step close",
    ]
    assert life.clock_now >= ACCEPT_TIME + BUILD_SECONDS
    assert len(life.spawned) == WIRE_SPAWNS


@pytest.mark.parametrize("exit_status", [0, 1], ids=["zero", "one"])
def test_every_exit_of_the_step_reaches_the_sender(
    connection: socket.socket, listener: socket.socket, exit_status: int
) -> None:
    life = accepted_life(step=ScriptedStep(exit_status=exit_status))

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CONSUMED
    assert sender_leg(life)[0] == "spawn"


def test_a_connection_readable_during_the_build_kills_the_step_and_ends_the_life(
    connection: socket.socket, listener: socket.socket
) -> None:
    life = accepted_life(step=ScriptedStep(connection_wakes=True))

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CANCELLED
    assert life.events[-3:] == ["step kill", "step wait", "step close"]
    assert len(life.spawned) == WIRE_SPAWNS - 1


def test_a_side_connection_during_the_build_is_closed_unread_and_the_step_goes_on(
    connection: socket.socket, listener: socket.socket
) -> None:
    step = ScriptedStep(listener_wakes=1)
    life = accepted_life(step=step)
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as side_client:
        side_client.settimeout(5)
        side_client.connect(listener.getsockname())

        outcome = run(life, connection, listener)

        assert side_client.recv(1) == b""
    assert outcome == LifeEnd.CONSUMED
    assert len(life.spawned) == WIRE_SPAWNS
    waits = life.budgets_waited[FIRST_STEP_WAIT : FIRST_STEP_WAIT + 2]
    assert waits == [BUILD_SECONDS, BUILD_SECONDS]


READY_AT_THE_CHECK = {
    "after the step exited zero": ScriptedStep(),
    "after the build bound ran out": ScriptedStep(stalled=True),
    "after the step's gate refused": ScriptedStep(gate_admits=False),
}


@pytest.mark.parametrize(
    "step", READY_AT_THE_CHECK.values(), ids=READY_AT_THE_CHECK.keys()
)
def test_a_connection_readable_at_the_last_check_kills_the_gated_sender_and_cancels(
    connection: socket.socket, listener: socket.socket, step: ScriptedStep
) -> None:
    life = accepted_life(step=step, connection_ready_at_check=True)

    outcome = run(life, connection, listener)

    assert outcome == LifeEnd.CANCELLED
    assert sender_leg(life) == [
        "spawn",
        "status",
        "connection-check",
        "kill",
        "wait",
        "close",
    ]
    assert life.connection_checks == [((life.spawned[0].pass_fds[0],), 0.0)]


BEFORE_THE_CHECK = {
    "the sender's gate refusing": (
        {"send_gate_admits": False},
        LifeEnd.GATE,
        ["spawn", "status", "kill", "wait", "close"],
    ),
    "the reply budget being spent": (
        {"send_spawn_takes": REPLY_SECONDS + 1, "send_stalled": True},
        LifeEnd.REPLY_EXPIRED,
        ["spawn", "status", "kill", "wait", "close"],
    ),
}


@pytest.mark.parametrize(
    ("changes", "life_end", "leg"),
    BEFORE_THE_CHECK.values(),
    ids=BEFORE_THE_CHECK.keys(),
)
def test_the_connection_is_not_looked_at_when_the_sender_is_already_ended(
    connection: socket.socket,
    listener: socket.socket,
    changes: dict[str, object],
    life_end: LifeEnd,
    leg: list[str],
) -> None:
    life = accepted_life(connection_ready_at_check=True, **changes)

    outcome = run(life, connection, listener)

    assert outcome == life_end
    assert sender_leg(life) == leg
