"""The serving loop admits only the runtime uid and ends the life by name."""

import errno
import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import ClassVar, NoReturn, overload

import pytest

from presentator.host import worker, worker_serving
from presentator.host.worker_serving import HEADER_SECONDS, LifeEnd
from presentator.host.worker_termination import SWEEP_SECONDS, SweepEffects, sweep
from presentator.host.worker_wire import WireExit
from tests.step_support import ADMITTING_PATH, STEP_LEG
from tests.worker_support import (
    ACCEPT_TIME,
    CHILD_LEG,
    CONNECTIONS,
    FOREIGN_UID,
    HELLO,
    IDLE_LEG,
    OWN_GID,
    OWN_UID,
    SENDER_LEG,
    STATUS,
    UNUSED_WORK,
    WAIT_SECONDS,
    BoundedListener,
    ScriptedLife,
    StopServingError,
    connect,
    connect_when_bound,
    listening_with,
    serving,
    wake_until_ended,
)


def test_the_runtime_uid_is_admitted() -> None:
    assert worker_serving.peer_admitted(OWN_UID, runtime_uid=OWN_UID)


@pytest.mark.parametrize(
    ("peer_uid", "runtime_uid"), [(0, OWN_UID), (FOREIGN_UID, OWN_UID), (0, 0)]
)
def test_any_other_peer_uid_is_refused_uid_zero_included(
    peer_uid: int, runtime_uid: int
) -> None:
    assert not worker_serving.peer_admitted(peer_uid, runtime_uid=runtime_uid)


class UnidentifiableConnection(socket.socket):
    """A connection whose peer credentials cannot be read."""

    accepted: ClassVar[list[socket.socket]] = []

    def getsockopt(self, *_arguments: int) -> NoReturn:
        raise OSError(errno.ENOTSOCK, "no peer credentials")


class RootConnection(socket.socket):
    """A connection whose peer is uid 0."""

    @overload
    def getsockopt(self, _level: int, _optname: int, /) -> int: ...
    @overload
    def getsockopt(self, _level: int, _optname: int, _buflen: int, /) -> bytes: ...
    def getsockopt(
        self, _level: int, _optname: int, _buflen: int | None = None, /
    ) -> int | bytes:
        return worker_serving.PEER_CREDENTIALS.pack(1, 0, 0)


class RootListener(BoundedListener):
    """A listener whose accepted connections come from uid 0."""

    def accept(self) -> tuple[socket.socket, object]:
        connection, address = super().accept()
        return RootConnection(fileno=connection.detach()), address


class UnidentifiableListener(BoundedListener):
    """A listener whose accepted connections have no readable peer credentials."""

    def accept(self) -> tuple[socket.socket, object]:
        connection, address = super().accept()
        unidentifiable = UnidentifiableConnection(fileno=connection.detach())
        UnidentifiableConnection.accepted.append(unidentifiable)
        return unidentifiable, address


def test_a_foreign_peer_is_reset_unread_and_never_spawns_a_child(
    volume: Path,
) -> None:
    life = ScriptedLife()
    with listening_with(volume, accepts=1) as listener:
        client = connect(volume)
        client.send(b"x")
        with pytest.raises(StopServingError):
            accept_from(life, listener, FOREIGN_UID)
        with pytest.raises(ConnectionResetError):
            client.recv(1)

    assert life.spawned == []


def test_a_connection_without_readable_credentials_is_closed_and_ends_the_life(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    life = ScriptedLife()
    monkeypatch.setattr(UnidentifiableConnection, "accepted", [])
    with listening_with(volume, 1, UnidentifiableListener) as listener:
        connect(volume)
        outcome = accept_from(life, listener, OWN_UID)

    assert outcome == LifeEnd.ACCEPT
    assert life.spawned == []
    assert life.events == ["arm 7.0"]
    assert [
        connection.fileno() for connection in UnidentifiableConnection.accepted
    ] == [-1]


def test_uid_zero_is_closed_at_the_accept_loop_even_when_it_is_the_runtime_uid(
    volume: Path,
) -> None:
    life = ScriptedLife()
    with listening_with(volume, 1, RootListener) as listener:
        client = connect(volume)
        with pytest.raises(StopServingError):
            accept_from(life, listener, 0)
        assert client.recv(1) == b""

    assert life.spawned == []


def test_a_refused_peer_is_swept_and_the_watchdog_cancelled_before_the_next_accept(
    volume: Path,
) -> None:
    life = ScriptedLife()
    with listening_with(volume, accepts=2) as listener:
        connect(volume)
        connect(volume)
        with pytest.raises(StopServingError):
            accept_from(life, listener, FOREIGN_UID)

    assert life.events == 2 * ["arm 7.0", "sweep 104.0", "cancel"]


@pytest.mark.parametrize("exit_status", [WireExit.CLOSED, WireExit.ANSWERED])
def test_a_kept_life_connection_is_handed_to_a_gated_child_then_swept(
    volume: Path, monkeypatch: pytest.MonkeyPatch, exit_status: WireExit
) -> None:
    monkeypatch.setenv("PATH", ADMITTING_PATH)
    life = ScriptedLife(exit_status=exit_status)
    with listening_with(volume, accepts=2) as listener:
        clients = [connect(volume), connect(volume)]
        with pytest.raises(StopServingError):
            accept_from(life, listener, OWN_UID)
        assert [client.recv(1) for client in clients] == [b"", b""]

    assert life.events == 2 * [
        "arm 7.0",
        "spawn",
        "status",
        "release",
        "wait-within",
        "close",
        "sweep 104.0",
        "cancel",
    ]
    assert [spawned.environment for spawned in life.spawned] == 2 * [
        {"PATH": ADMITTING_PATH}
    ]


def test_the_child_is_told_the_descriptor_of_its_connection_and_inherits_only_it(
    volume: Path,
) -> None:
    life = ScriptedLife()
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        with pytest.raises(StopServingError):
            accept_from(life, listener, OWN_UID)

    [spawned] = life.spawned
    assert spawned.command[1:] == [
        "-I",
        "-m",
        "presentator.host.worker_wire",
        "--role",
        "idle",
        "--connection-fd",
        str(spawned.pass_fds[0]),
        "--record-fd",
        str(spawned.pass_fds[1]),
        "--work",
        str(UNUSED_WORK),
        "--megabytes",
        "300",
    ]


def accept_from(
    life: ScriptedLife, listener: socket.socket, runtime_uid: int
) -> LifeEnd:
    return worker_serving.accept_connections(listener, runtime_uid, life.serving())


def test_a_sweep_that_is_quiet_only_after_its_deadline_ends_the_life_armed(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", ADMITTING_PATH)
    life = ScriptedLife()
    late = SweepEffects(
        kill_all=lambda: None,
        reap=lambda: None,
        list_process_ids=lambda: ["1"],
        clock=lambda: ACCEPT_TIME + SWEEP_SECONDS + 1,
        sleep=lambda _seconds: None,
    )
    late_serving = replace(life.serving(), sweep=partial(sweep, late))
    with listening_with(volume, accepts=2) as listener:
        connect(volume)
        connect(volume)
        outcome = worker_serving.accept_connections(listener, OWN_UID, late_serving)

    assert outcome == LifeEnd.SWEEP
    assert "cancel" not in life.events


ACCEPTED_REARMS = {
    "the design's 300 s build, observed at the accept": (300.0, 0.0, 447.0, 105.0),
    "the design's 1 s build, observed at the accept": (1.0, 0.0, 148.0, 105.0),
    "an observation 1.5 s after the accept shortens it by 1.5 s": (
        1.0,
        1.5,
        146.5,
        108.0,
    ),
}


@pytest.mark.parametrize(
    ("build_timeout", "inspection_takes", "rearmed_for", "quiesce_deadline"),
    ACCEPTED_REARMS.values(),
    ids=ACCEPTED_REARMS.keys(),
)
def test_an_accepted_step_re_arms_the_watchdog_to_the_emergency_end_from_the_accept(
    volume: Path,
    build_timeout: float,
    inspection_takes: float,
    rearmed_for: float,
    quiesce_deadline: float,
) -> None:
    life = ScriptedLife(
        exit_status=WireExit.ACCEPTED,
        build_timeout_seconds=build_timeout,
        inspection_takes=inspection_takes,
    )
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        accept_from(life, listener, OWN_UID)

    assert life.events == [
        "arm 7.0",
        *IDLE_LEG,
        f"arm {rearmed_for}",
        "seal",
        "open-deck",
        *CHILD_LEG,
        "close-deck",
        *STEP_LEG,
        f"sweep {quiesce_deadline}",
        *SENDER_LEG,
    ]


def consume_an_accepted_step(volume: Path, life: ScriptedLife) -> None:
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        accept_from(life, listener, OWN_UID)


def test_every_role_is_spawned_with_the_work_and_the_megabytes_of_the_serving(
    volume: Path,
) -> None:
    life = ScriptedLife(
        exit_status=WireExit.ACCEPTED, build_output_megabytes=7, work=Path("/a")
    )

    consume_an_accepted_step(volume, life)

    idle, upload, sender = life.spawned
    assert [spawned.command[-4:] for spawned in (idle, upload)] == [
        ["--work", "/a", "--megabytes", "7"]
    ] * 2
    assert sender.command[-6:] == ["--work", "/a", "--megabytes", "7", "--plan", "walk"]
    assert [
        spawned.command[spawned.command.index("--role") + 1] for spawned in life.spawned
    ] == [
        "idle",
        "upload",
        "send",
    ]


def test_the_deck_effects_receive_the_deck_below_the_work_root_of_the_serving(
    volume: Path,
) -> None:
    life = ScriptedLife(exit_status=WireExit.ACCEPTED, work=Path("/a"))

    consume_an_accepted_step(volume, life)

    assert life.decks_handled == [Path("/a/deck")] * 2


LIFE_ENDS = {
    "a spawn failure": (
        lambda: ScriptedLife(spawn_error=OSError("no process")),
        LifeEnd.SPAWN,
        ["arm 7.0", "spawn"],
    ),
    "a gate mismatch": (
        lambda: ScriptedLife(gate_admits=False),
        LifeEnd.GATE,
        ["arm 7.0", "spawn", "status", "kill", "wait", "close"],
    ),
    "an unreadable status": (
        lambda: ScriptedLife(status=OSError("gone")),
        LifeEnd.GATE,
        ["arm 7.0", "spawn", "status", "kill", "wait", "close"],
    ),
    "a broken pipe at the release": (
        lambda: ScriptedLife(release_error=BrokenPipeError()),
        LifeEnd.GATE,
        ["arm 7.0", "spawn", "status", "release", "kill", "wait", "close"],
    ),
    "an exit status outside the kept set": (
        lambda: ScriptedLife(exit_status=1),
        LifeEnd.WIRE_EXIT,
        ["arm 7.0", "spawn", "status", "release", "wait-within", "close"],
    ),
    "a child killed by a signal": (
        lambda: ScriptedLife(exit_status=-9),
        LifeEnd.WIRE_EXIT,
        ["arm 7.0", "spawn", "status", "release", "wait-within", "close"],
    ),
    "an accepted step": (
        lambda: ScriptedLife(exit_status=WireExit.ACCEPTED),
        LifeEnd.CONSUMED,
        [
            "arm 7.0",
            *IDLE_LEG,
            "arm 447.0",
            "seal",
            "open-deck",
            *CHILD_LEG,
            "close-deck",
            *STEP_LEG,
            "sweep 105.0",
            *SENDER_LEG,
        ],
    ),
    "a sweep that is not quiet": (
        lambda: ScriptedLife(sweep_quiet=False),
        LifeEnd.SWEEP,
        [
            "arm 7.0",
            "spawn",
            "status",
            "release",
            "wait-within",
            "close",
            "sweep 104.0",
        ],
    ),
}


@pytest.mark.parametrize(
    ("make_life", "life_end", "events"), LIFE_ENDS.values(), ids=LIFE_ENDS.keys()
)
def test_a_failing_step_ends_the_life_and_leaves_the_watchdog_armed(
    volume: Path,
    make_life: Callable[[], ScriptedLife],
    life_end: LifeEnd,
    events: list[str],
) -> None:
    life = make_life()
    with listening_with(volume, accepts=1) as listener:
        client = connect(volume)
        outcome = accept_from(life, listener, OWN_UID)
        assert client.recv(1) == b""

    assert outcome == life_end
    assert life.events == events


@pytest.mark.parametrize(
    ("make_life", "life_end", "_events"), LIFE_ENDS.values(), ids=LIFE_ENDS.keys()
)
def test_serve_returns_zero_and_logs_the_name_of_what_ended_the_life_at_its_level(
    volume: Path,
    caplog: pytest.LogCaptureFixture,
    make_life: Callable[[], ScriptedLife],
    life_end: LifeEnd,
    _events: list[str],
) -> None:
    caplog.set_level(logging.INFO)
    life = make_life()
    outcome: list[int] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            worker.serve(
                volume,
                controller_uid=OWN_UID,
                controller_gid=OWN_GID,
                runtime_uid=OWN_UID,
                serving=life.serving(),
            ),
        ),
        daemon=True,
    )
    thread.start()
    try:
        connect_when_bound(volume, thread.is_alive).close()
        thread.join(WAIT_SECONDS)
    finally:
        life.spawn_error = OSError("stop")
        wake_until_ended(volume, thread)

    assert outcome == [0]
    ended = [r for r in caplog.records if r.getMessage() == f"life ended: {life_end}"]
    expected = logging.INFO if life_end is LifeEnd.CONSUMED else logging.ERROR
    assert [r.levelno for r in ended] == [expected]


def test_a_sweep_that_fails_for_another_reason_surfaces_with_its_error(
    volume: Path,
) -> None:
    life = ScriptedLife(sweep_error=PermissionError("kill refused"))
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        with pytest.raises(PermissionError, match="kill refused"):
            accept_from(life, listener, OWN_UID)

    assert "cancel" not in life.events


class FailingAcceptListener(BoundedListener):
    """A listener whose accept fails as an exhausted descriptor table does."""

    def accept(self) -> NoReturn:
        raise OSError(errno.EMFILE, "too many open files")


def test_a_failing_accept_ends_the_life_before_anything_is_armed_spawned_or_swept(
    volume: Path,
) -> None:
    life = ScriptedLife()
    with listening_with(volume, 0, FailingAcceptListener) as listener:
        outcome = accept_from(life, listener, OWN_UID)

    assert outcome == LifeEnd.ACCEPT
    assert life.events == []


def test_serve_returns_zero_and_names_the_accept_fault(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bind_socket = worker.bind_socket

    def bind_failing_accept(
        directory: Path, controller_uid: int, controller_gid: int
    ) -> FailingAcceptListener:
        plain = bind_socket(directory, controller_uid, controller_gid)
        return FailingAcceptListener(fileno=plain.detach())

    monkeypatch.setattr(worker, "bind_socket", bind_failing_accept)

    status = worker.serve(
        volume,
        controller_uid=OWN_UID,
        controller_gid=OWN_GID,
        runtime_uid=OWN_UID,
        serving=ScriptedLife().serving(),
    )

    assert status == 0
    assert f"life ended: {LifeEnd.ACCEPT}" in caplog.text


def test_the_runtime_uid_gets_one_gated_child_per_connection(volume: Path) -> None:
    life = ScriptedLife()
    with listening_with(volume, accepts=CONNECTIONS) as listener:
        clients = [connect(volume), connect(volume)]
        with pytest.raises(StopServingError):
            accept_from(life, listener, OWN_UID)

    assert len(life.spawned) == len(clients)


def test_serve_binds_listens_and_hands_over_connections_until_the_life_ends(
    volume: Path,
) -> None:
    with serving(volume, OWN_UID, admitted=CONNECTIONS) as (thread, handled, outcome):
        for _ in range(CONNECTIONS):
            client = connect_when_bound(volume, thread.is_alive)
            assert client.recv(1) == b""
            client.close()
        thread.join(WAIT_SECONDS)

        assert not thread.is_alive()
        assert len(handled) == CONNECTIONS
        assert outcome == [0]


def test_a_failure_inside_serving_still_ends_the_serving_thread(volume: Path) -> None:
    threads: list[threading.Thread] = []

    def fail_before_any_connection() -> None:
        with serving(volume, OWN_UID, admitted=CONNECTIONS) as (thread, _, _):
            threads.append(thread)
            raise StopServingError

    with pytest.raises(StopServingError):
        fail_before_any_connection()

    assert not threads[0].is_alive()


def test_an_unexpected_serving_error_surfaces_to_the_caller(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = RuntimeError("serving broke")

    def broken(*_arguments: object, **_keywords: object) -> NoReturn:
        raise failure

    monkeypatch.setattr(worker, "serve", broken)

    with (
        pytest.raises(RuntimeError) as surfaced,
        serving(volume, OWN_UID, admitted=CONNECTIONS),
    ):
        pass

    assert surfaced.value is failure


def test_a_body_failure_carries_the_serving_error_as_a_note(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serving_failure = RuntimeError("serving broke")
    body_failure = AssertionError("body broke")

    def broken(*_arguments: object, **_keywords: object) -> NoReturn:
        raise serving_failure

    monkeypatch.setattr(worker, "serve", broken)

    with (
        pytest.raises(AssertionError) as surfaced,
        serving(volume, OWN_UID, admitted=CONNECTIONS),
    ):
        raise body_failure

    assert surfaced.value is body_failure
    assert any("serving broke" in note for note in body_failure.__notes__)


CUT_OFF_HEADER = [
    "arm 7.0",
    "spawn",
    "status",
    "release",
    "wait-within",
    "kill",
    "wait",
    "close",
    "sweep 104.0",
    "cancel",
]
ANSWERED_HEADER = [
    "arm 7.0",
    "spawn",
    "status",
    "release",
    "wait-within",
    "close",
    "sweep 104.0",
    "cancel",
]
EXPIRED_BEFORE_RELEASE = [
    "arm 7.0",
    "spawn",
    "status",
    "kill",
    "wait",
    "close",
    "sweep 104.0",
]
EXPIRED_AT_RELEASE = [
    "arm 7.0",
    "spawn",
    "status",
    "release",
    "kill",
    "wait",
    "close",
    "sweep 104.0",
]


def accept_twice_from(life: ScriptedLife, volume: Path) -> list[bytes]:
    with listening_with(volume, accepts=2) as listener:
        clients = [connect(volume), connect(volume)]
        with pytest.raises(StopServingError):
            accept_from(life, listener, OWN_UID)
        return [client.recv(1) for client in clients]


def test_a_header_that_outlasts_its_budget_is_cut_off_and_the_next_connection_is_served(
    volume: Path,
) -> None:
    life = ScriptedLife(headers_stalled=1)

    replies = accept_twice_from(life, volume)

    assert replies == [b"", b""]
    assert life.budgets_waited == [HEADER_SECONDS, HEADER_SECONDS]
    assert life.events == CUT_OFF_HEADER + ANSWERED_HEADER


def test_a_stalled_header_never_ends_the_life_while_the_sweep_is_quiet(
    volume: Path,
) -> None:
    life = ScriptedLife(headers_stalled=2)
    with listening_with(volume, accepts=2) as listener:
        connect(volume)
        connect(volume)
        with pytest.raises(StopServingError):
            accept_from(life, listener, OWN_UID)

    assert life.events == 2 * CUT_OFF_HEADER


def test_serve_cuts_a_stalled_header_off_and_serves_the_next_connection_in_one_life(
    volume: Path,
) -> None:
    life = ScriptedLife(
        headers_stalled=1,
        connections_served_before_sweep_fails=2,
        answer=STATUS,
    )
    outcome: list[int] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            worker.serve(
                volume,
                controller_uid=OWN_UID,
                controller_gid=OWN_GID,
                runtime_uid=OWN_UID,
                serving=life.serving(),
            )
        ),
        daemon=True,
    )
    thread.start()
    try:
        first = connect_when_bound(volume, thread.is_alive)
        assert first.recv(1) == b""
        second = connect(volume)
        second.sendall(HELLO)
        assert second.recv(len(STATUS)) == STATUS
        thread.join(WAIT_SECONDS)
    finally:
        life.spawn_error = OSError("stop")
        wake_until_ended(volume, thread)

    assert outcome == [0]
    assert len(life.spawned) == CONNECTIONS
    assert life.events == CUT_OFF_HEADER + ANSWERED_HEADER[:-1]


@pytest.mark.parametrize(
    ("inspection_takes", "release_takes", "events", "budgets"),
    [
        (HEADER_SECONDS, 0.0, [*EXPIRED_BEFORE_RELEASE, "cancel"], []),
        (HEADER_SECONDS + 1, 0.0, [*EXPIRED_BEFORE_RELEASE, "cancel"], []),
        (0.0, HEADER_SECONDS, [*EXPIRED_AT_RELEASE, "cancel"], []),
        (HEADER_SECONDS * 0.25, 0.0, ANSWERED_HEADER, [HEADER_SECONDS * 0.75]),
    ],
    ids=[
        "an inspection that spends the whole budget",
        "an inspection that overruns the budget",
        "a release that spends the rest of the budget",
        "an inspection that leaves a quarter of the budget spent",
    ],
)
def test_the_header_budget_covers_the_gate_and_the_release_as_well_as_the_wait(
    volume: Path,
    inspection_takes: float,
    release_takes: float,
    events: list[str],
    budgets: list[float],
) -> None:
    life = ScriptedLife(inspection_takes=inspection_takes, release_takes=release_takes)
    with listening_with(volume, accepts=1) as listener:
        client = connect(volume)
        with pytest.raises(StopServingError):
            accept_from(life, listener, OWN_UID)
        assert client.recv(1) == b""

    assert life.events == events
    assert life.budgets_waited == budgets


@pytest.mark.parametrize("sweep_quiet", [True, False], ids=["quiet", "not quiet"])
def test_the_life_is_kept_after_an_expiry_only_when_the_sweep_is_quiet(
    volume: Path, *, sweep_quiet: bool
) -> None:
    life = ScriptedLife(inspection_takes=HEADER_SECONDS, sweep_quiet=sweep_quiet)
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        if sweep_quiet:
            with pytest.raises(StopServingError):
                accept_from(life, listener, OWN_UID)
        else:
            assert accept_from(life, listener, OWN_UID) == LifeEnd.SWEEP

    assert ("cancel" in life.events) == sweep_quiet


@pytest.mark.parametrize("sweep_quiet", [True, False], ids=["quiet", "not quiet"])
def test_a_stall_that_spends_the_header_budget_before_the_launch_spawns_no_idle_child(
    volume: Path, *, sweep_quiet: bool
) -> None:
    life = ScriptedLife(arm_takes=HEADER_SECONDS, sweep_quiet=sweep_quiet)
    with listening_with(volume, accepts=1) as listener:
        client = connect(volume)
        if sweep_quiet:
            with pytest.raises(StopServingError):
                accept_from(life, listener, OWN_UID)
        else:
            assert accept_from(life, listener, OWN_UID) == LifeEnd.SWEEP
        assert client.recv(1) == b""

    assert life.spawned == []
    assert ("cancel" in life.events) == sweep_quiet


def test_a_gate_mismatch_is_a_gate_end_whatever_the_clock_says(volume: Path) -> None:
    life = ScriptedLife(gate_admits=False, inspection_takes=HEADER_SECONDS)
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        outcome = accept_from(life, listener, OWN_UID)

    assert outcome == LifeEnd.GATE
    assert "release" not in life.events
