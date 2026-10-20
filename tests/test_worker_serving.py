"""The build worker binds its socket in the volume."""

import errno
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from presentator.host import worker
from presentator.host.worker import StartRefusal
from tests.worker_support import (
    FOREIGN_UID,
    OWN_GID,
    OWN_UID,
    PRIVATE_MODE,
    ScriptedLife,
    connect,
)

SUPPLEMENTARY_GIDS = tuple(gid for gid in os.getgroups() if gid != OWN_GID)


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


def test_refuses_a_stale_socket_it_cannot_remove(volume: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(volume / worker.SOCKET_NAME))
    stale.close()
    volume.chmod(0o500)

    with pytest.raises(worker.StartRefusedError) as refused:
        worker.remove_stale_socket(volume / worker.SOCKET_NAME, OWN_UID)
    volume.chmod(0o700)

    assert refused.value.refusal == StartRefusal.STALE_SOCKET
