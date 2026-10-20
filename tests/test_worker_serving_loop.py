"""The serving loop admits only the runtime uid and ends the life by name."""

import errno
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import ClassVar, NoReturn, overload

import pytest

from presentator.host import worker, worker_serving
from presentator.host.worker_serving import LifeEnd
from presentator.host.worker_termination import SWEEP_SECONDS, SweepEffects, sweep
from presentator.host.worker_wire import WireExit
from tests.worker_support import (
    ACCEPT_TIME,
    ADMITTING_PATH,
    CONNECTIONS,
    FOREIGN_UID,
    OWN_GID,
    OWN_UID,
    WAIT_SECONDS,
    BoundedListener,
    ScriptedLife,
    StopServingError,
    connect,
    connect_when_bound,
    listening_with,
    serving,
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


def test_a_connection_without_readable_credentials_is_closed_and_the_error_surfaces(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    life = ScriptedLife()
    monkeypatch.setattr(UnidentifiableConnection, "accepted", [])
    with listening_with(volume, 1, UnidentifiableListener) as listener:
        connect(volume)
        with pytest.raises(OSError, match="no peer credentials"):
            accept_from(life, listener, OWN_UID)

    assert life.spawned == []
    assert "cancel" not in life.events
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
        "wait",
        "close-gate",
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
        "--connection-fd",
        str(spawned.pass_fds[0]),
    ]
    assert len(spawned.pass_fds) == 1


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


LIFE_ENDS = {
    "a spawn failure": (
        lambda: ScriptedLife(spawn_error=OSError("no process")),
        LifeEnd.SPAWN,
        ["arm 7.0", "spawn"],
    ),
    "a gate mismatch": (
        lambda: ScriptedLife(gate_admits=False),
        LifeEnd.GATE,
        ["arm 7.0", "spawn", "status", "kill", "wait", "close-gate"],
    ),
    "an unreadable status": (
        lambda: ScriptedLife(status=OSError("gone")),
        LifeEnd.GATE,
        ["arm 7.0", "spawn", "status", "kill", "wait", "close-gate"],
    ),
    "a broken pipe at the release": (
        lambda: ScriptedLife(release_error=BrokenPipeError()),
        LifeEnd.GATE,
        ["arm 7.0", "spawn", "status", "release", "kill", "wait", "close-gate"],
    ),
    "an exit status outside the kept set": (
        lambda: ScriptedLife(exit_status=1),
        LifeEnd.WIRE_EXIT,
        ["arm 7.0", "spawn", "status", "release", "wait", "close-gate"],
    ),
    "a child killed by a signal": (
        lambda: ScriptedLife(exit_status=-9),
        LifeEnd.WIRE_EXIT,
        ["arm 7.0", "spawn", "status", "release", "wait", "close-gate"],
    ),
    "a sweep that is not quiet": (
        lambda: ScriptedLife(sweep_quiet=False),
        LifeEnd.SWEEP,
        [
            "arm 7.0",
            "spawn",
            "status",
            "release",
            "wait",
            "close-gate",
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
def test_serve_returns_zero_and_logs_the_name_of_what_ended_the_life(
    volume: Path,
    caplog: pytest.LogCaptureFixture,
    make_life: Callable[[], ScriptedLife],
    life_end: LifeEnd,
    _events: list[str],
) -> None:
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
    connect_when_bound(volume, thread).close()
    thread.join(WAIT_SECONDS)

    assert outcome == [0]
    assert f"life ended: {life_end}" in caplog.text


def test_a_sweep_that_fails_for_another_reason_surfaces_with_its_error(
    volume: Path,
) -> None:
    life = ScriptedLife(sweep_error=PermissionError("kill refused"))
    with listening_with(volume, accepts=1) as listener:
        connect(volume)
        with pytest.raises(PermissionError, match="kill refused"):
            accept_from(life, listener, OWN_UID)

    assert "cancel" not in life.events


def test_the_watchdog_is_armed_before_the_peer_check_and_left_armed_when_it_fails(
    volume: Path,
) -> None:
    life = ScriptedLife()
    with listening_with(volume, 1, UnidentifiableListener) as listener:
        connect(volume)
        with pytest.raises(OSError, match="no peer credentials"):
            accept_from(life, listener, OWN_UID)

    assert life.events == ["arm 7.0"]


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
            client = connect_when_bound(volume, thread)
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
