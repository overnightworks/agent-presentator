"""The build worker binds its socket in the volume and admits only the runtime uid."""

import errno
import os
import socket
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from presentator.host import worker
from presentator.host.worker import StartRefusal

OWN_UID = os.getuid()
OWN_GID = os.getgid()
FOREIGN_UID = OWN_UID + 1
PRIVATE_MODE = 0o700
CONNECTIONS = 2


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


def run_serve(
    volume: Path, runtime_uid: int, handled: list[socket.socket], admitted: int
) -> tuple[threading.Thread, list[object]]:
    outcome: list[object] = []

    def handle(connection: socket.socket) -> None:
        handled.append(connection)
        worker.close_unread(connection)
        if len(handled) == admitted:
            raise StopServingError

    def run() -> None:
        try:
            outcome.append(
                worker.serve(
                    volume,
                    controller_uid=OWN_UID,
                    controller_gid=OWN_GID,
                    runtime_uid=runtime_uid,
                    handle=handle,
                )
            )
        except StopServingError:
            outcome.append("stopped")

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def connect(volume: Path) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(volume / worker.SOCKET_NAME))
    return client


def connect_when_bound(volume: Path) -> socket.socket:
    while True:
        try:
            return connect(volume)
        except (FileNotFoundError, ConnectionRefusedError):
            continue


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


def test_refuses_a_volume_root_that_is_a_symlink_or_a_file(
    tmp_path_factory: pytest.TempPathFactory, volume: Path
) -> None:
    link = tmp_path_factory.mktemp("l") / "link"
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


def test_refuses_a_socket_the_controller_does_not_own_after_bind(volume: Path) -> None:
    with pytest.raises(worker.StartRefusedError) as refused:
        worker.bind_socket(volume, FOREIGN_UID, OWN_GID)

    assert refused.value.refusal == StartRefusal.BIND


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
        handle=worker.close_unread,
    )

    assert status == worker.REFUSAL_EXIT_STATUS
    assert f"start refused: {refusal}" in caplog.text


def test_the_runtime_uid_is_admitted() -> None:
    assert worker.peer_admitted(OWN_UID, runtime_uid=OWN_UID)


@pytest.mark.parametrize("peer_uid", [0, FOREIGN_UID])
def test_any_other_peer_uid_is_refused_uid_zero_included(peer_uid: int) -> None:
    assert not worker.peer_admitted(peer_uid, runtime_uid=OWN_UID)


class UnreadableConnection(socket.socket):
    """A connection that counts reads."""

    reads = 0

    def recv(self, *_arguments: object) -> bytes:
        UnreadableConnection.reads += 1
        return b""


class BoundedListener(socket.socket):
    """A listener that stops the accept loop after a fixed number of accepts."""

    accepts_left = 0

    def accept(self) -> tuple[socket.socket, object]:
        if self.accepts_left == 0:
            raise StopServingError
        self.accepts_left -= 1
        connection, address = super().accept()
        return UnreadableConnection(fileno=connection.detach()), address


def listening_with(volume: Path, accepts: int) -> BoundedListener:
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    plain = worker.bind_socket(volume, OWN_UID, OWN_GID)
    listener = BoundedListener(fileno=plain.detach())
    listener.accepts_left = accepts
    worker.start_listening(listener)
    return listener


def test_a_foreign_peer_is_closed_unread_and_never_reaches_the_handler(
    volume: Path,
) -> None:
    handled: list[socket.socket] = []
    UnreadableConnection.reads = 0
    with listening_with(volume, accepts=1) as listener:
        client = connect(volume)
        with pytest.raises(StopServingError):
            worker.accept_connections(listener, FOREIGN_UID, handled.append)
        assert client.recv(1) == b""

    assert handled == []
    assert UnreadableConnection.reads == 0


def test_the_runtime_uid_is_handed_to_the_handler_once_per_connection(
    volume: Path,
) -> None:
    handled: list[socket.socket] = []
    with listening_with(volume, accepts=CONNECTIONS) as listener:
        clients = [connect(volume), connect(volume)]
        with pytest.raises(StopServingError):
            worker.accept_connections(listener, OWN_UID, handled.append)

    assert len(handled) == len(clients)


def test_serve_binds_listens_and_hands_over_connections_until_stopped(
    volume: Path,
) -> None:
    handled: list[socket.socket] = []
    thread, outcome = run_serve(volume, OWN_UID, handled, admitted=CONNECTIONS)

    for _ in range(CONNECTIONS):
        client = connect_when_bound(volume)
        assert client.recv(1) == b""
        client.close()
    thread.join(5)

    assert len(handled) == CONNECTIONS
    assert outcome == ["stopped"]


def test_refuses_a_stale_socket_it_cannot_remove(volume: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(volume / worker.SOCKET_NAME))
    stale.close()
    volume.chmod(0o500)

    with pytest.raises(worker.StartRefusedError) as refused:
        worker.remove_stale_socket(volume / worker.SOCKET_NAME, OWN_UID)
    volume.chmod(0o700)

    assert refused.value.refusal == StartRefusal.STALE_SOCKET
