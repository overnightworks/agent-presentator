"""The U and R waits as one loop over the child's exit and the listener, on a fake."""

import errno
import logging
import socket
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace

import pytest

from presentator.host.worker_serving import (
    REPLY_SECONDS,
    UPLOAD_SECONDS,
    LifeEnd,
    hand_over,
)
from presentator.host.worker_wire import WireExit, WireRole
from tests.worker_support import (
    ACCEPT_TIME,
    IDLE_LEG,
    WAIT_SECONDS,
    ScriptedLife,
    role_of,
)

BOTH_PHASES = (WireRole.UPLOAD, WireRole.SEND)
FLOOD = 10_000
QUEUED_WAKES = 2


class SideConnectionReadError(AssertionError):
    """A side connection was read, which the controller must never do."""


class UnreadConnection(socket.socket):
    """An accepted side connection that fails when anything reads it."""

    def __getattribute__(self, name: str) -> object:
        """Refuse every receive method."""
        if name.startswith("recv"):
            raise SideConnectionReadError
        return super().__getattribute__(name)


class RecordingListener(socket.socket):
    """A listener that counts its accepts and hands out unreadable connections."""

    accept_calls = 0
    accepted: list[UnreadConnection]
    refusal: OSError | None = None
    restore_failure: OSError | None = None

    def settimeout(self, value: float | None) -> None:
        if value is None and self.restore_failure is not None:
            raise self.restore_failure
        super().settimeout(value)

    def accept(self) -> tuple[socket.socket, object]:
        self.accept_calls += 1
        if self.refusal is not None:
            raise self.refusal
        connection, address = super().accept()
        side = UnreadConnection(fileno=connection.detach())
        self.accepted.append(side)
        return side, address


@pytest.fixture
def recording(listener: socket.socket) -> Iterator[RecordingListener]:
    watched = RecordingListener(fileno=listener.detach())
    watched.accepted = []
    with closing(watched):
        yield watched


def accepted_life(**changes: object) -> ScriptedLife:
    return replace(ScriptedLife(exit_status=WireExit.ACCEPTED), **changes)


def roles_spawned(life: ScriptedLife) -> list[str]:
    return [role_of(spawned.command) for spawned in life.spawned]


def stalled(role: WireRole) -> dict[str, bool]:
    return {f"{role}_stalled": True}


@pytest.mark.parametrize("role", BOTH_PHASES)
def test_a_side_connection_during_a_phase_is_accepted_and_closed_unread(
    role: WireRole, connection: socket.socket, recording: RecordingListener
) -> None:
    life = accepted_life(listener_wakes=1, scripted_roles=(role,))
    side_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    side_client.settimeout(WAIT_SECONDS)
    with closing(side_client):
        side_client.connect(recording.getsockname())

        outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

        assert side_client.recv(1) == b""
    assert outcome == LifeEnd.CONSUMED
    assert recording.accept_calls == 1
    assert [side.fileno() for side in recording.accepted] == [-1]
    assert recording.gettimeout() is None
    assert roles_spawned(life) == ["idle", "upload", "send"]
    assert sum(event.startswith("arm") for event in life.events) == 1
    assert "cancel" not in life.events


@pytest.mark.parametrize(
    ("role", "expired"),
    [(WireRole.UPLOAD, LifeEnd.UPLOAD_EXPIRED), (WireRole.SEND, LifeEnd.REPLY_EXPIRED)],
)
def test_a_flood_of_side_connections_cannot_move_the_end_of_a_phase(
    role: WireRole,
    expired: LifeEnd,
    connection: socket.socket,
    recording: RecordingListener,
) -> None:
    budget = UPLOAD_SECONDS if role == WireRole.UPLOAD else REPLY_SECONDS
    life = accepted_life(
        listener_wakes=FLOOD,
        scripted_roles=(role,),
        wake_takes=0.5,
        **stalled(role),
    )

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == expired
    assert recording.accept_calls == FLOOD - life.listener_wakes
    assert life.clock_now == pytest.approx(ACCEPT_TIME + budget, abs=0.5)


@pytest.mark.parametrize("role", BOTH_PHASES)
def test_an_exit_in_the_wake_of_a_waiting_listener_wins_and_leaves_it_queued(
    role: WireRole, connection: socket.socket, recording: RecordingListener
) -> None:
    life = accepted_life(
        listener_wakes=1, scripted_roles=(role,), exit_with_listener=True
    )

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == LifeEnd.CONSUMED
    assert recording.accept_calls == 0
    assert roles_spawned(life) == ["idle", "upload", "send"]


@pytest.mark.parametrize(
    "runs_to_the_deadline",
    [{"upload_runs_for": UPLOAD_SECONDS}, {"send_runs_for": REPLY_SECONDS}],
    ids=["upload", "reply"],
)
def test_an_exit_seen_in_the_wake_that_reaches_the_deadline_counts_as_the_exit(
    runs_to_the_deadline: dict[str, float],
    connection: socket.socket,
    recording: RecordingListener,
) -> None:
    life = accepted_life(**runs_to_the_deadline)

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == LifeEnd.CONSUMED
    assert "kill" not in life.events


@pytest.mark.parametrize("role", BOTH_PHASES)
def test_nothing_pending_on_the_listener_closes_nothing_and_the_wait_goes_on(
    role: WireRole, connection: socket.socket, recording: RecordingListener
) -> None:
    life = accepted_life(listener_wakes=QUEUED_WAKES, scripted_roles=(role,))

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == LifeEnd.CONSUMED
    assert recording.accept_calls == QUEUED_WAKES
    assert recording.accepted == []
    assert recording.gettimeout() is None


def test_a_failing_accept_is_logged_once_ends_the_listening_for_that_wait_only(
    connection: socket.socket,
    recording: RecordingListener,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recording.refusal = OSError(errno.EMFILE, "too many open files")
    life = accepted_life(listener_wakes=4, scripted_roles=BOTH_PHASES)

    with caplog.at_level(logging.ERROR):
        outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == LifeEnd.CONSUMED
    assert recording.accept_calls == len(BOTH_PHASES)
    assert len([record for record in caplog.records if record.exc_info]) == len(
        BOTH_PHASES
    )


def test_a_side_connection_is_closed_even_when_the_listener_cannot_be_restored(
    connection: socket.socket, recording: RecordingListener
) -> None:
    recording.restore_failure = OSError(errno.EBADF, "bad file descriptor")
    life = accepted_life(listener_wakes=1)
    side_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    side_client.settimeout(WAIT_SECONDS)
    with closing(side_client):
        side_client.connect(recording.getsockname())

        with pytest.raises(OSError, match="bad file descriptor"):
            hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

        assert side_client.recv(1) == b""
    assert [side.fileno() for side in recording.accepted] == [-1]


@pytest.mark.parametrize("role", BOTH_PHASES)
def test_every_wake_waits_only_for_the_rest_of_the_phase(
    role: WireRole, connection: socket.socket, recording: RecordingListener
) -> None:
    budget = UPLOAD_SECONDS if role == WireRole.UPLOAD else REPLY_SECONDS
    life = accepted_life(
        listener_wakes=2, scripted_roles=(role,), wake_takes=1.0, empty_wakes=1
    )

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == LifeEnd.CONSUMED
    first_wake = 1 if role == WireRole.UPLOAD else 2
    waited = life.budgets_waited[first_wake : first_wake + 4]
    assert waited == [budget - taken for taken in range(4)]


@pytest.mark.parametrize(
    ("role", "expired"),
    [(WireRole.UPLOAD, LifeEnd.UPLOAD_EXPIRED), (WireRole.SEND, LifeEnd.REPLY_EXPIRED)],
)
def test_an_empty_wake_at_the_end_of_the_phase_expires_it(
    role: WireRole,
    expired: LifeEnd,
    connection: socket.socket,
    recording: RecordingListener,
) -> None:
    life = accepted_life(**stalled(role))

    outcome = hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    assert outcome == expired
    killed = life.events.index("kill")
    assert life.events[killed + 1] == "wait"


def test_the_idle_phase_waits_within_and_never_reads_readiness(
    connection: socket.socket, recording: RecordingListener
) -> None:
    life = accepted_life()

    hand_over(connection, life.serving(), ACCEPT_TIME, listener=recording)

    idle_leg = life.events[: life.events.index("seal")]
    assert [event for event in idle_leg if not event.startswith("arm")] == IDLE_LEG
