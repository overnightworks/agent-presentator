"""The reply phase R: the sender's spawn, deadline and the life it ends."""

import socket
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace

import pytest

from presentator.host.worker_serving import REPLY_SECONDS, LifeEnd, hand_over
from presentator.host.worker_wire import WireExit
from tests.worker_support import ACCEPT_TIME, CHILD_LEG, ScriptedLife

SENDER_WAIT = 2


@pytest.fixture
def connection() -> Iterator[socket.socket]:
    controller_end, client = socket.socketpair()
    with closing(controller_end), closing(client):
        yield controller_end


def accepted_life(**changes: object) -> ScriptedLife:
    return replace(ScriptedLife(exit_status=WireExit.ACCEPTED), **changes)


def test_the_sender_runs_after_the_deck_closed_and_its_exit_sent_consumes_the_life(
    connection: socket.socket,
) -> None:
    life = accepted_life()

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME)

    assert outcome == LifeEnd.CONSUMED
    assert life.events[life.events.index("close-deck") :] == ["close-deck", *CHILD_LEG]
    assert [
        spawned.command[spawned.command.index("--role") + 1] for spawned in life.spawned
    ] == [
        "idle",
        "upload",
        "send",
    ]


SENDER_CONSUMERS = {
    "the sender's spawn": {"send_spawn_takes": 0.35},
    "the sender's gate check": {"inspection_takes": 0.5},
    "the sender's release": {"release_takes": 0.25},
}


@pytest.mark.parametrize(
    "consumed", SENDER_CONSUMERS.values(), ids=SENDER_CONSUMERS.keys()
)
def test_the_sender_wait_is_what_remains_of_the_reply_budget(
    connection: socket.socket, consumed: dict[str, float]
) -> None:
    life = accepted_life(**consumed)

    hand_over(connection, life.serving(), ACCEPT_TIME)

    [taken] = consumed.values()
    assert life.budgets_waited[SENDER_WAIT] == pytest.approx(REPLY_SECONDS - taken)


def test_the_reply_budget_starts_after_the_upload_exited_not_at_the_accept(
    connection: socket.socket,
) -> None:
    life = accepted_life(upload_runs_for=50.0)

    hand_over(connection, life.serving(), ACCEPT_TIME)

    assert life.budgets_waited[SENDER_WAIT] == pytest.approx(REPLY_SECONDS)


def test_a_stalled_sender_is_killed_and_reaped_and_the_life_ends_reply_expired(
    connection: socket.socket,
) -> None:
    life = accepted_life(send_stalled=True)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME)

    assert outcome == LifeEnd.REPLY_EXPIRED
    assert life.events[life.events.index("close-deck") + 1 :] == [
        "spawn",
        "status",
        "release",
        "wait-within",
        "kill",
        "wait",
        "close-gate",
    ]


def test_a_reply_budget_spent_before_the_release_prevents_the_release(
    connection: socket.socket,
) -> None:
    life = accepted_life(send_spawn_takes=REPLY_SECONDS + 1, send_stalled=True)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME)

    assert outcome == LifeEnd.REPLY_EXPIRED
    assert life.events[life.events.index("close-deck") + 1 :] == [
        "spawn",
        "status",
        "kill",
        "wait",
        "close-gate",
    ]


SENDER_FAILURES: dict[str, tuple[dict[str, object], LifeEnd, list[str]]] = {
    "a spawn failure": ({"send_spawn_error": OSError("no")}, LifeEnd.SPAWN, ["spawn"]),
    "a gate mismatch": (
        {"send_gate_admits": False},
        LifeEnd.GATE,
        ["spawn", "status", "kill", "wait", "close-gate"],
    ),
    "an unreadable status": (
        {"send_status": OSError("gone")},
        LifeEnd.GATE,
        ["spawn", "status", "kill", "wait", "close-gate"],
    ),
    "a broken pipe at the release": (
        {"send_release_error": BrokenPipeError()},
        LifeEnd.GATE,
        ["spawn", "status", "release", "kill", "wait", "close-gate"],
    ),
}


@pytest.mark.parametrize(
    ("changes", "life_end", "leg"), SENDER_FAILURES.values(), ids=SENDER_FAILURES.keys()
)
def test_a_sender_that_cannot_run_ends_the_life_and_is_killed_before_it_runs(
    connection: socket.socket,
    changes: dict[str, object],
    life_end: LifeEnd,
    leg: list[str],
) -> None:
    life = accepted_life(**changes)

    assert hand_over(connection, life.serving(), ACCEPT_TIME) == life_end

    assert life.events[life.events.index("close-deck") + 1 :] == leg


@pytest.mark.parametrize("exit_status", [WireExit.CLOSED, 1, -9])
def test_a_sender_exit_other_than_sent_ends_the_life_wire_exit(
    connection: socket.socket, exit_status: int
) -> None:
    life = accepted_life(send_exit_status=exit_status)

    assert hand_over(connection, life.serving(), ACCEPT_TIME) == LifeEnd.WIRE_EXIT
