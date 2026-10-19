"""The build worker binds its socket in the volume and admits only the runtime uid."""

import errno
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, NoReturn, overload

import pytest

from presentator.host import worker
from presentator.host.worker import LifeEnd, Serving, StartRefusal
from presentator.host.worker_wire import WireExit

OWN_UID = os.getuid()
OWN_GID = os.getgid()
FOREIGN_UID = OWN_UID + 1
PRIVATE_MODE = 0o700
CONNECTIONS = 2
WAIT_SECONDS = 5
SUPPLEMENTARY_GIDS = tuple(gid for gid in os.getgroups() if gid != OWN_GID)


class StopServingError(Exception):
    """Ends an accept loop the test bounds."""


@pytest.fixture
def volume(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    before = os.umask(0)
    os.umask(before)
    directory = tmp_path_factory.mktemp("v")
    directory.chmod(PRIVATE_MODE)
    yield directory
    os.umask(before)


GATED_STATUS = "Uid:\t10002\t10002\t10002\t10002"
ACCEPT_TIME = 100.0
ADMITTING_PATH = "/opt/test/bin"


@dataclass(frozen=True)
class Spawned:
    """What the controller asked a spawn for."""

    command: Sequence[str]
    environment: Mapping[str, str]
    pass_fds: Sequence[int]


@dataclass
class ScriptedLife:
    """One serving life's effects and its wire child, recorded in order."""

    events: list[str] = field(default_factory=list[str])
    spawn_error: OSError | None = None
    status: str | OSError = GATED_STATUS
    gate_admits: bool = True
    release_error: OSError | None = None
    exit_status: int = WireExit.CLOSED
    sweep_error: OSError | None = None
    sweep_quiet: bool = True
    connections_served_before_sweep_fails: int | None = None
    spawned: list[Spawned] = field(default_factory=list[Spawned])

    def spawn(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> "ScriptedLife":
        self.spawned.append(Spawned(command, environment, pass_fds))
        self.events.append("spawn")
        if self.spawn_error is not None:
            raise self.spawn_error
        return self

    def status_text(self) -> str:
        self.events.append("status")
        if isinstance(self.status, OSError):
            raise self.status
        return self.status

    def release(self) -> None:
        self.events.append("release")
        if self.release_error is not None:
            raise self.release_error

    def kill(self) -> None:
        self.events.append("kill")

    def wait(self) -> int:
        self.events.append("wait")
        return self.exit_status

    def close_gate(self) -> None:
        self.events.append("close-gate")

    def sweep(self, deadline: float) -> bool:
        self.events.append(f"sweep {deadline}")
        if self.sweep_error is not None:
            raise self.sweep_error
        served_enough = (
            self.connections_served_before_sweep_fails is not None
            and len(self.spawned) >= self.connections_served_before_sweep_fails
        )
        return self.sweep_quiet and not served_enough

    def arm(self, remaining: float) -> None:
        self.events.append(f"arm {remaining}")

    def cancel(self) -> None:
        self.events.append("cancel")

    def serving(self) -> Serving:
        return Serving(
            spawn=self.spawn,
            gate_admits=lambda _status: self.gate_admits,
            sweep=self.sweep,
            watchdog=self,
            clock=lambda: ACCEPT_TIME,
        )


@contextmanager
def serving(
    volume: Path, runtime_uid: int, admitted: int
) -> Generator[tuple[threading.Thread, list[Spawned], list[object]]]:
    life = ScriptedLife(connections_served_before_sweep_fails=admitted)
    handled = life.spawned
    outcome: list[object] = []

    def run() -> None:
        outcome.append(
            worker.serve(
                volume,
                controller_uid=OWN_UID,
                controller_gid=OWN_GID,
                runtime_uid=runtime_uid,
                serving=life.serving(),
            )
        )

    thread = ServingThread(target=run, daemon=True)

    def finish() -> None:
        life.spawn_error = OSError("cancelled")
        wake_until_ended(volume, thread)
        assert not thread.is_alive()

    try:
        thread.start()
        yield thread, handled, outcome
    except BaseException as body_error:
        finish()
        if thread.failure is not None:
            body_error.add_note(f"the serving thread also failed: {thread.failure!r}")
        raise
    finish()
    if thread.failure is not None:
        raise thread.failure


class ServingThread(threading.Thread):
    """Keeps the error that ended the thread while pytest still sees it."""

    failure: BaseException | None = None

    def run(self) -> None:
        try:
            super().run()
        except BaseException as error:
            self.failure = error
            raise


def wake_until_ended(volume: Path, thread: threading.Thread) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while thread.is_alive() and (time_left := deadline - time.monotonic()) > 0:
        with suppress(OSError):
            connect(volume, timeout=time_left).close()
        if (time_left := deadline - time.monotonic()) > 0:
            thread.join(min(0.05, time_left))


def connect(volume: Path, timeout: float = WAIT_SECONDS) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(volume / worker.SOCKET_NAME))
    return client


def connect_when_bound(volume: Path, thread: threading.Thread) -> socket.socket:
    deadline = time.monotonic() + WAIT_SECONDS
    while thread.is_alive() and time.monotonic() < deadline:
        try:
            return connect(volume)
        except (FileNotFoundError, ConnectionRefusedError):
            continue
    pytest.fail("the worker did not start serving")


def make_regular_file(path: Path) -> None:
    path.write_text("")


def make_symlink(path: Path) -> None:
    path.symlink_to(path.parent)


def make_dangling_symlink(path: Path) -> None:
    path.symlink_to(path.parent / "missing")


def make_directory(path: Path) -> None:
    path.mkdir()


@pytest.mark.parametrize(
    "occupy", [make_regular_file, make_symlink, make_dangling_symlink, make_directory]
)
def test_refuses_a_non_socket_at_the_socket_path_and_leaves_it(
    volume: Path, occupy: Callable[[Path], None]
) -> None:
    occupied = volume / worker.SOCKET_NAME
    occupy(occupied)

    with pytest.raises(worker.StartRefusedError) as refused:
        worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)

    assert refused.value.refusal == StartRefusal.STALE_SOCKET
    assert occupied.is_symlink() or occupied.exists()


def test_replaces_a_stale_socket_of_the_controller(volume: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(volume / worker.SOCKET_NAME))
    stale.close()

    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)

    assert not (volume / worker.SOCKET_NAME).exists()


def test_refuses_a_foreign_socket_at_the_socket_path(volume: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(volume / worker.SOCKET_NAME))
    stale.close()

    with pytest.raises(worker.StartRefusedError) as refused:
        worker.remove_stale_socket(volume / worker.SOCKET_NAME, FOREIGN_UID)

    assert refused.value.refusal == StartRefusal.STALE_SOCKET
    assert (volume / worker.SOCKET_NAME).exists()


def test_refuses_a_volume_root_of_another_uid_and_leaves_its_mode(volume: Path) -> None:
    with pytest.raises(worker.StartRefusedError) as refused:
        worker.prepare_socket_directory(volume, FOREIGN_UID, OWN_GID)

    assert refused.value.refusal == StartRefusal.SOCKET_DIRECTORY
    assert stat.S_IMODE(volume.stat().st_mode) == PRIVATE_MODE


def test_refuses_a_volume_root_that_is_a_symlink_or_a_file(volume: Path) -> None:
    link = volume / "link"
    link.symlink_to(volume)
    plain_file = volume / "file"
    plain_file.write_text("")

    for root in (link, plain_file):
        with pytest.raises(worker.StartRefusedError) as refused:
            worker.prepare_socket_directory(root, OWN_UID, OWN_GID)
        assert refused.value.refusal == StartRefusal.SOCKET_DIRECTORY
    assert stat.S_IMODE(volume.stat().st_mode) == PRIVATE_MODE


def test_refuses_a_missing_volume_root(volume: Path) -> None:
    with pytest.raises(worker.StartRefusedError) as refused:
        worker.prepare_socket_directory(volume / "missing", OWN_UID, OWN_GID)

    assert refused.value.refusal == StartRefusal.SOCKET_DIRECTORY


def test_binds_a_group_socket_in_a_group_directory(volume: Path) -> None:
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    with worker.bind_socket(volume, OWN_UID, OWN_GID):
        socket_status = (volume / worker.SOCKET_NAME).lstat()
        directory_status = volume.stat()

    assert stat.S_IMODE(socket_status.st_mode) == worker.SOCKET_MODE
    assert (socket_status.st_uid, socket_status.st_gid) == (OWN_UID, OWN_GID)
    assert stat.S_IMODE(directory_status.st_mode) == worker.SOCKET_DIRECTORY_MODE
    assert directory_status.st_gid == OWN_GID


@pytest.mark.parametrize(
    ("controller_uid", "controller_gid", "bind_umask"),
    [
        (FOREIGN_UID, OWN_GID, worker.BIND_UMASK),
        (OWN_UID, OWN_GID + 1, worker.BIND_UMASK),
        (OWN_UID, OWN_GID, 0o077),
    ],
    ids=["foreign-uid", "foreign-gid", "wrong-mode"],
)
def test_refuses_a_socket_the_controller_does_not_own_after_bind(
    volume: Path,
    monkeypatch: pytest.MonkeyPatch,
    controller_uid: int,
    controller_gid: int,
    bind_umask: int,
) -> None:
    monkeypatch.setattr(worker, "BIND_UMASK", bind_umask)
    with pytest.raises(worker.StartRefusedError) as refused:
        worker.bind_socket(volume, controller_uid, controller_gid)

    assert refused.value.refusal == StartRefusal.BIND
    assert stat.S_ISSOCK((volume / worker.SOCKET_NAME).lstat().st_mode)


def test_refuses_the_start_when_no_socket_can_be_allocated(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def exhausted(*_arguments: object) -> socket.socket:
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(worker.socket, "socket", exhausted)

    status = worker.serve(
        volume,
        controller_uid=OWN_UID,
        controller_gid=OWN_GID,
        runtime_uid=OWN_UID,
        serving=ScriptedLife().serving(),
    )

    assert status == worker.REFUSAL_EXIT_STATUS
    assert f"start refused: {StartRefusal.BIND}" in caplog.text


@pytest.mark.skipif(
    not SUPPLEMENTARY_GIDS, reason="the runner has no supplementary group"
)
def test_hands_the_volume_root_to_the_controller_group(volume: Path) -> None:
    controller_gid = SUPPLEMENTARY_GIDS[0]

    worker.prepare_socket_directory(volume, OWN_UID, controller_gid)

    assert volume.stat().st_gid == controller_gid


def test_connect_is_refused_between_bind_and_listen_and_succeeds_after(
    volume: Path,
) -> None:
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    with worker.bind_socket(volume, OWN_UID, OWN_GID) as listener:
        with pytest.raises(ConnectionRefusedError) as early:
            connect(volume)
        worker.start_listening(listener)
        connect(volume).close()

    assert early.value.errno == errno.ECONNREFUSED


def test_umask_is_owner_only_after_a_successful_bind(volume: Path) -> None:
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    worker.bind_socket(volume, OWN_UID, OWN_GID).close()

    assert os.umask(0) == worker.ONLY_THE_OWNER


def test_umask_is_owner_only_when_the_bind_raises(volume: Path) -> None:
    with pytest.raises(worker.StartRefusedError) as refused:
        worker.bind_socket(volume / "missing", OWN_UID, OWN_GID)

    assert refused.value.refusal == StartRefusal.BIND
    assert os.umask(0) == worker.ONLY_THE_OWNER


def test_listen_failure_is_a_named_refusal() -> None:
    unbound = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    with unbound, pytest.raises(worker.StartRefusedError) as refused:
        worker.start_listening(unbound)

    assert refused.value.refusal == StartRefusal.LISTEN


@pytest.mark.parametrize(
    ("controller_uid", "controller_gid", "refusal"),
    [
        (FOREIGN_UID, OWN_GID, StartRefusal.SOCKET_DIRECTORY),
        (OWN_UID, OWN_GID, StartRefusal.BIND),
    ],
    ids=["step-4", "step-5"],
)
def test_serve_exits_two_and_logs_the_name_of_the_refused_step(
    volume: Path,
    caplog: pytest.LogCaptureFixture,
    controller_uid: int,
    controller_gid: int,
    refusal: StartRefusal,
) -> None:
    if refusal == StartRefusal.BIND:
        volume = volume / ("d" * 120)
        volume.mkdir()
    status = worker.serve(
        volume,
        controller_uid=controller_uid,
        controller_gid=controller_gid,
        runtime_uid=OWN_UID,
        serving=ScriptedLife().serving(),
    )

    assert status == worker.REFUSAL_EXIT_STATUS
    assert f"start refused: {refusal}" in caplog.text


def test_the_runtime_uid_is_admitted() -> None:
    assert worker.peer_admitted(OWN_UID, runtime_uid=OWN_UID)


@pytest.mark.parametrize(
    ("peer_uid", "runtime_uid"), [(0, OWN_UID), (FOREIGN_UID, OWN_UID), (0, 0)]
)
def test_any_other_peer_uid_is_refused_uid_zero_included(
    peer_uid: int, runtime_uid: int
) -> None:
    assert not worker.peer_admitted(peer_uid, runtime_uid=runtime_uid)


class BoundedListener(socket.socket):
    """A listener that stops the accept loop after a fixed number of accepts."""

    accepts_left = 0

    def accept(self) -> tuple[socket.socket, object]:
        if self.accepts_left == 0:
            raise StopServingError
        self.accepts_left -= 1
        return super().accept()


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
        return worker.PEER_CREDENTIALS.pack(1, 0, 0)


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


def listening_with(
    volume: Path, accepts: int, listener_class: type[BoundedListener] = BoundedListener
) -> BoundedListener:
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    plain = worker.bind_socket(volume, OWN_UID, OWN_GID)
    listener = listener_class(fileno=plain.detach())
    listener.accepts_left = accepts
    worker.start_listening(listener)
    return listener


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


def test_a_kept_life_connection_is_handed_to_a_gated_child_then_swept(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", ADMITTING_PATH)
    life = ScriptedLife()
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
    return worker.accept_connections(listener, runtime_uid, life.serving())


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
    "an exit status that is not CLOSED": (
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
            )
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


def test_refuses_a_stale_socket_it_cannot_remove(volume: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(volume / worker.SOCKET_NAME))
    stale.close()
    volume.chmod(0o500)

    with pytest.raises(worker.StartRefusedError) as refused:
        worker.remove_stale_socket(volume / worker.SOCKET_NAME, OWN_UID)
    volume.chmod(0o700)

    assert refused.value.refusal == StartRefusal.STALE_SOCKET
