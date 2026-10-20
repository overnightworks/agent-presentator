"""The build worker: it refuses to start outside the sealed container, then serves."""

import argparse
import ctypes
import fcntl
import grp
import logging
import os
import pwd
import signal
import socket
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntFlag, StrEnum
from functools import partial
from pathlib import Path
from typing import Protocol

from presentator.adapters.worker_children import ChildIdentity, spawn_gated
from presentator.host.config import (
    ConfigurationError,
    WorkerSettings,
    load_worker_settings,
    worker_settings_variables,
)
from presentator.host.worker_serving import Serving, accept_connections
from presentator.host.worker_termination import (
    FaultHandlerWatchdog,
    SweepEffects,
    list_process_ids,
    reap_children,
    sweep,
)

logger = logging.getLogger(__name__)

REFUSAL_EXIT_STATUS = 2
WORKER_CAPABILITIES = 0xE0
MAXIMUM_PIDS = 512
RUNTIME_USER = "presentator"
BUILD_USER = "presentator-build"
WIRE_USER = "presentator-wire"
BUILD_IDENTITY = (10001, 10001)
WIRE_IDENTITY = (10002, 10002)
NO_CAPABILITIES = "0" * 16
ROOT_MODE = 0o755
WORK_MOUNT = "/work"
SOCKET_DIRECTORY = "/run/presentator-build"
SOCKET_NAME = "worker.sock"
SOCKET_DIRECTORY_MODE = 0o750
SOCKET_MODE = 0o660
BIND_UMASK = 0o117
LISTEN_BACKLOG = 4
# Mount points below the root, so the self-check names no world-writable path.
SCRATCH_MOUNT_NAMES = ("tmp", "dev/shm", SOCKET_DIRECTORY.lstrip("/"))
TMPFS_MOUNTS = tuple(
    str(Path("/") / name) for name in (WORK_MOUNT.lstrip("/"), *SCRATCH_MOUNT_NAMES)
)
NO_EXECUTION_MOUNTS = TMPFS_MOUNTS[:2]
GROUP_AND_OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH
ONLY_THE_OWNER = 0o077
HARDENED_MOUNT_OPTIONS = frozenset({"nosuid", "nodev", "noexec"})
DOCKER_SOCKETS = ("/run/docker.sock", "/var/run/docker.sock")
SLIDEV_BINARY = "app/frontend/node_modules/.bin/slidev"
SLIDEV_PACKAGE = "app/frontend/node_modules/@slidev/cli"
COREPACK_DIRECTORY = "opt/corepack"
HEADLESS_SHELL_PATTERN = (
    "opt/ms-playwright/chromium_headless_shell-*/"
    "chrome-headless-shell-linux64/chrome-headless-shell"
)
VITE_CACHE_TARGET = "/work/vite"
# The image's ENV keys, the base image's keys and the container runtime's HOSTNAME.
ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "PLAYWRIGHT_BROWSERS_PATH",
        "COREPACK_HOME",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON_INSTALL_DIR",
        "UV_COMPILE_BYTECODE",
        "UV_NO_CACHE",
        "PATH",
        "PRESENTATOR_DATABASE",
        "PRESENTATOR_MIRRORS",
        "PRESENTATOR_BUILDS",
        "PRESENTATOR_TOOLCHAIN",
        "PRESENTATOR_HOST",
        "PRESENTATOR_LOCAL_SOURCES_MOUNT",
        "NODE_VERSION",
        "YARN_VERSION",
        "HOSTNAME",
    }
    | worker_settings_variables()
)

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4


class Predicate(StrEnum):
    """A self-check predicate, named in refusal order."""

    NON_DUMPABLE = "non-dumpable"
    CAPABILITIES = "capabilities"
    MOUNTS = "mounts"
    RESOURCE_BOUNDS = "resource-bounds"
    IDENTITIES = "identities"
    ENVIRONMENT = "environment"
    PID_ONE = "pid-one"
    TOOLCHAIN = "toolchain"


class StartRefusal(StrEnum):
    """A serving step that refused the start."""

    SOCKET_DIRECTORY = "socket-directory"
    STALE_SOCKET = "stale-socket"
    BIND = "bind"
    LISTEN = "listen"
    SETTINGS = "settings"


class StartRefusedError(Exception):
    """A serving step refused; it carries the step's name."""

    def __init__(self, refusal: StartRefusal) -> None:
        """Remember which step refused."""
        super().__init__(refusal)
        self.refusal = refusal


@dataclass(frozen=True)
class Identity:
    """The process identity and the users the image created."""

    uid: int
    euid: int
    gid: int
    supplementary_gids: tuple[int, ...]
    runtime_uid: int
    runtime_gid: int
    users: Mapping[str, tuple[int, int]]


@dataclass(frozen=True)
class Toolchain:
    """What the sealed container holds of the build toolchain."""

    slidev_mode: int | None
    corepack_mode: int | None
    headless_shell_modes: tuple[int | None, ...]
    vite_link_target: str | None


@dataclass(frozen=True)
class Snapshot:
    """Everything the self-check reads, taken once."""

    dumpable: int
    process_status: str
    root_read_only: bool
    mountinfo: str
    work_mode: int
    work_owner_uid: int
    network_devices: str
    docker_socket_present: bool
    pids_max: str
    memory_max: str
    identity: Identity
    environment_keys: frozenset[str]
    pid: int
    toolchain: Toolchain
    unreadable: frozenset[Predicate] = frozenset()


class Server(Protocol):
    """Serves the socket for the runtime uid and the controller group."""

    def __call__(
        self, *, controller_gid: int, runtime_uid: int, settings: WorkerSettings
    ) -> int:
        """Run until a refusal and return its exit status."""
        ...


@dataclass(frozen=True)
class Boundary:
    """The process effects and reads the start-up steps go through."""

    set_non_dumpable: Callable[[], None]
    set_umask: Callable[[], None]
    read_snapshot: Callable[[], Snapshot]
    load_settings: Callable[[], WorkerSettings]
    serve: Server


def _status_fields(process_status: str) -> dict[str, str]:
    fields = (line.partition(":") for line in process_status.splitlines())
    return {name: value.strip() for name, _, value in fields}


def _capabilities_sealed(snapshot: Snapshot) -> bool:
    fields = _status_fields(snapshot.process_status)
    worker = f"{WORKER_CAPABILITIES:016x}"
    return (
        fields.get("CapEff") == worker
        and fields.get("CapPrm") == worker
        and fields.get("CapInh") == NO_CAPABILITIES
        and fields.get("CapAmb") == NO_CAPABILITIES
        and int(fields.get("CapBnd", "-1"), 16) & ~WORKER_CAPABILITIES == 0
        and fields.get("NoNewPrivs") == "1"
        and fields.get("Seccomp") == "2"
    )


def _mount_table(mountinfo: str) -> dict[str, tuple[str, frozenset[str]]]:
    table: dict[str, tuple[str, frozenset[str]]] = {}
    for line in mountinfo.splitlines():
        head, _, tail = line.partition(" - ")
        fields = head.split()
        table[fields[4]] = (tail.split()[0], frozenset(fields[5].split(",")))
    return table


def _tmpfs_mounts_sealed(mountinfo: str) -> bool:
    table = _mount_table(mountinfo)
    return all(
        mount in table and table[mount][0] == "tmpfs" for mount in TMPFS_MOUNTS
    ) and all(
        table[mount][1] >= HARDENED_MOUNT_OPTIONS for mount in NO_EXECUTION_MOUNTS
    )


def _mounts_sealed(snapshot: Snapshot) -> bool:
    interfaces = {
        line.partition(":")[0].strip()
        for line in snapshot.network_devices.splitlines()[2:]
    }
    return (
        snapshot.root_read_only
        and _tmpfs_mounts_sealed(snapshot.mountinfo)
        and snapshot.work_mode == ROOT_MODE
        and snapshot.work_owner_uid == 0
        and interfaces == {"lo"}
        and not snapshot.docker_socket_present
    )


def _resources_bounded(snapshot: Snapshot) -> bool:
    return (
        snapshot.pids_max.isdecimal()
        and int(snapshot.pids_max) <= MAXIMUM_PIDS
        and snapshot.memory_max.isdecimal()
    )


def _identities_sealed(snapshot: Snapshot) -> bool:
    identity = snapshot.identity
    build_user = identity.users.get(BUILD_USER)
    wire_user = identity.users.get(WIRE_USER)
    unusable = {0, identity.runtime_uid, identity.runtime_gid}
    return (
        identity.uid == 0
        and identity.euid == 0
        and identity.gid == identity.runtime_gid
        and identity.supplementary_gids == (identity.runtime_gid,)
        and build_user == BUILD_IDENTITY
        and wire_user == WIRE_IDENTITY
        and not unusable & {*BUILD_IDENTITY, *WIRE_IDENTITY}
    )


def _environment_allowed(snapshot: Snapshot) -> bool:
    return snapshot.environment_keys <= ALLOWED_ENVIRONMENT_KEYS


def _is_pid_one(snapshot: Snapshot) -> bool:
    return snapshot.pid == 1


def _toolchain_present(snapshot: Snapshot) -> bool:
    toolchain = snapshot.toolchain
    entries = (
        toolchain.slidev_mode,
        toolchain.corepack_mode,
        *toolchain.headless_shell_modes,
    )
    return (
        len(toolchain.headless_shell_modes) == 1
        and all(
            mode is not None
            and mode & stat.S_IXOTH
            and not mode & GROUP_AND_OTHERS_WRITE
            for mode in entries
        )
        and toolchain.vite_link_target == VITE_CACHE_TARGET
    )


SELF_CHECK: tuple[tuple[Predicate, Callable[[Snapshot], bool]], ...] = (
    (Predicate.NON_DUMPABLE, lambda snapshot: snapshot.dumpable == 0),
    (Predicate.CAPABILITIES, _capabilities_sealed),
    (Predicate.MOUNTS, _mounts_sealed),
    (Predicate.RESOURCE_BOUNDS, _resources_bounded),
    (Predicate.IDENTITIES, _identities_sealed),
    (Predicate.ENVIRONMENT, _environment_allowed),
    (Predicate.PID_ONE, _is_pid_one),
    (Predicate.TOOLCHAIN, _toolchain_present),
)


def _holds(
    predicate: Predicate, judge: Callable[[Snapshot], bool], snapshot: Snapshot
) -> bool:
    if predicate in snapshot.unreadable:
        return False
    try:
        return judge(snapshot)
    except (ValueError, IndexError):
        return False


def first_failing_predicate(snapshot: Snapshot) -> Predicate | None:
    """The first predicate, in refusal order, that the snapshot fails.

    A predicate whose facts could not be read or parsed fails.
    """
    return next(
        (name for name, judge in SELF_CHECK if not _holds(name, judge, snapshot)),
        None,
    )


def libc_prctl(option: int, value: int) -> int:
    """Call prctl, raising the errno it sets on failure."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *[ctypes.c_ulong] * 4]
    libc.prctl.restype = ctypes.c_int
    result = libc.prctl(option, value, 0, 0, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "prctl failed")
    return result


def _set_non_dumpable() -> None:
    libc_prctl(PR_SET_DUMPABLE, 0)


def _set_umask() -> None:
    os.umask(ONLY_THE_OWNER)


def _mode_if_file_or_directory(path: Path) -> int | None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    return mode if stat.S_ISREG(mode) or stat.S_ISDIR(mode) else None


def _vite_link_target(root: Path) -> str | None:
    vite = root / SLIDEV_PACKAGE / "node_modules" / ".vite"
    try:
        return str(vite.readlink())
    except OSError:
        return None


def _read_toolchain(root: Path) -> Toolchain:
    slidev_binary = root / SLIDEV_BINARY
    return Toolchain(
        slidev_mode=_mode_if_file_or_directory(slidev_binary),
        corepack_mode=_mode_if_file_or_directory(root / COREPACK_DIRECTORY),
        headless_shell_modes=tuple(
            map(_mode_if_file_or_directory, root.glob(HEADLESS_SHELL_PATTERN))
        ),
        vite_link_target=_vite_link_target(root),
    )


def _read_identity() -> Identity:
    runtime = pwd.getpwnam(RUNTIME_USER)
    users = {
        user.pw_name: (user.pw_uid, user.pw_gid)
        for user in map(pwd.getpwnam, (BUILD_USER, WIRE_USER))
    }
    return Identity(
        uid=os.getuid(),
        euid=os.geteuid(),
        gid=os.getgid(),
        supplementary_gids=tuple(os.getgroups()),
        runtime_uid=runtime.pw_uid,
        runtime_gid=grp.getgrnam(RUNTIME_USER).gr_gid,
        users=users,
    )


def _lstat_if_present(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _read_mount_facts(root: Path) -> tuple[bool, str, int, int, str, bool]:
    work = (root / WORK_MOUNT.lstrip("/")).stat()
    return (
        bool(os.statvfs(root).f_flag & os.ST_RDONLY),
        (root / "proc/self/mountinfo").read_text(),
        stat.S_IMODE(work.st_mode),
        work.st_uid,
        (root / "proc/net/dev").read_text(),
        any(
            _lstat_if_present(root / docker_socket.lstrip("/")) is not None
            for docker_socket in DOCKER_SOCKETS
        ),
    )


def _read_resource_bounds(root: Path) -> tuple[str, str]:
    cgroup = root / "sys/fs/cgroup"
    return (
        (cgroup / "pids.max").read_text().strip(),
        (cgroup / "memory.max").read_text().strip(),
    )


UNREAD_IDENTITY = Identity(-1, -1, -1, (), -1, -1, {})
UNREAD_TOOLCHAIN = Toolchain(None, None, (), None)


def read_snapshot(root: Path) -> Snapshot:
    """Read the facts the predicates judge, from the filesystem under `root`.

    A fact that cannot be read or decoded marks its predicate unreadable, so the
    self-check judges every predicate in order instead of stopping at the first
    read.
    """
    unreadable: set[Predicate] = set()

    def read[Fact](
        predicate: Predicate, reading: Callable[[], Fact], placeholder: Fact
    ) -> Fact:
        try:
            return reading()
        except (OSError, KeyError, UnicodeDecodeError):
            unreadable.add(predicate)
            return placeholder

    dumpable = read(Predicate.NON_DUMPABLE, partial(libc_prctl, PR_GET_DUMPABLE, 0), -1)
    process_status = read(
        Predicate.CAPABILITIES, (root / "proc/self/status").read_text, ""
    )
    (
        root_read_only,
        mountinfo,
        work_mode,
        work_owner_uid,
        network_devices,
        socket_present,
    ) = read(
        Predicate.MOUNTS,
        partial(_read_mount_facts, root),
        (False, "", 0, -1, "", True),
    )
    pids_max, memory_max = read(
        Predicate.RESOURCE_BOUNDS, partial(_read_resource_bounds, root), ("", "")
    )
    return Snapshot(
        dumpable=dumpable,
        process_status=process_status,
        root_read_only=root_read_only,
        mountinfo=mountinfo,
        work_mode=work_mode,
        work_owner_uid=work_owner_uid,
        network_devices=network_devices,
        docker_socket_present=socket_present,
        pids_max=pids_max,
        memory_max=memory_max,
        identity=read(Predicate.IDENTITIES, _read_identity, UNREAD_IDENTITY),
        environment_keys=frozenset(os.environ),
        pid=os.getpid(),
        toolchain=read(
            Predicate.TOOLCHAIN, partial(_read_toolchain, root), UNREAD_TOOLCHAIN
        ),
        unreadable=frozenset(unreadable),
    )


def _fix_up_socket_directory(
    directory: Path, controller_uid: int, controller_gid: int
) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(descriptor).st_uid != controller_uid:
                raise StartRefusedError(StartRefusal.SOCKET_DIRECTORY)
            os.fchown(descriptor, -1, controller_gid)
            os.fchmod(descriptor, SOCKET_DIRECTORY_MODE)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise StartRefusedError(StartRefusal.SOCKET_DIRECTORY) from error


def remove_stale_socket(socket_path: Path, controller_uid: int) -> None:
    """Unlink a socket the controller owns; refuse any other entry."""
    try:
        status = _lstat_if_present(socket_path)
        if status is None:
            return
        if not (stat.S_ISSOCK(status.st_mode) and status.st_uid == controller_uid):
            raise StartRefusedError(StartRefusal.STALE_SOCKET)
        socket_path.unlink()
    except OSError as error:
        raise StartRefusedError(StartRefusal.STALE_SOCKET) from error


def prepare_socket_directory(
    directory: Path, controller_uid: int, controller_gid: int
) -> None:
    """Step 4: fix up the volume root and clear a stale socket."""
    _fix_up_socket_directory(directory, controller_uid, controller_gid)
    remove_stale_socket(directory / SOCKET_NAME, controller_uid)


def bind_socket(
    directory: Path, controller_uid: int, controller_gid: int
) -> socket.socket:
    """Step 5: bind under a narrow umask and check what the bind created."""
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        raise StartRefusedError(StartRefusal.BIND) from error
    socket_path = directory / SOCKET_NAME
    try:
        os.umask(BIND_UMASK)
        try:
            listener.bind(str(socket_path))
        finally:
            _set_umask()
        status = socket_path.lstat()
    except OSError as error:
        listener.close()
        raise StartRefusedError(StartRefusal.BIND) from error
    created_as_expected = (
        stat.S_ISSOCK(status.st_mode)
        and (status.st_uid, status.st_gid) == (controller_uid, controller_gid)
        and stat.S_IMODE(status.st_mode) == SOCKET_MODE
    )
    if not created_as_expected:
        listener.close()
        raise StartRefusedError(StartRefusal.BIND)
    return listener


def start_listening(listener: socket.socket) -> None:
    """Step 6: from here on a connect is queued rather than refused."""
    try:
        listener.listen(LISTEN_BACKLOG)
    except OSError as error:
        raise StartRefusedError(StartRefusal.LISTEN) from error


def wire_gate_admits(process_status: str, identity: tuple[int, int]) -> bool:
    """A wire child may be released only as the identity, unprivileged and sealed."""
    uid, gid = identity
    fields = _status_fields(process_status)
    return (
        fields.get("Uid", "").split() == [str(uid)] * 4
        and fields.get("Gid", "").split() == [str(gid)] * 4
        and fields.get("Groups") == ""
        and all(
            fields.get(name) == NO_CAPABILITIES
            for name in ("CapInh", "CapPrm", "CapEff", "CapAmb")
        )
        and fields.get("NoNewPrivs") == "1"
        and fields.get("Seccomp") == "2"
    )


def serve(
    directory: Path,
    *,
    controller_uid: int,
    controller_gid: int,
    runtime_uid: int,
    serving: Serving,
) -> int:
    """Run start-up steps 4 to 6, then accept until the life ends."""
    try:
        prepare_socket_directory(directory, controller_uid, controller_gid)
        listener = bind_socket(directory, controller_uid, controller_gid)
        with listener:
            start_listening(listener)
            life_end = accept_connections(listener, runtime_uid, serving)
    except StartRefusedError as refused:
        return _refuse(refused.refusal)
    logger.error("life ended: %s", life_end)
    return 0


# The Python standard library lacks these Linux constants (linux/fcntl.h).
F_ADD_SEALS = 1033
MFD_CLOEXEC = 1
MFD_ALLOW_SEALING = 2


class RecordSeal(IntFlag):
    """The memfd seals that freeze the request record."""

    SEAL = 1
    SHRINK = 2
    GROW = 4
    WRITE = 8


RECORD_SEALS = RecordSeal.WRITE | RecordSeal.GROW | RecordSeal.SHRINK | RecordSeal.SEAL


def create_request_record() -> int:
    """A sealable, close-on-exec memfd; the standalone Python has no os.memfd_create."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    libc.memfd_create.restype = ctypes.c_int
    descriptor = libc.memfd_create(b"request-record", MFD_CLOEXEC | MFD_ALLOW_SEALING)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "memfd_create failed")
    return descriptor


def seal_record(descriptor: int) -> None:
    """Freeze the request record; EBUSY while a writable shared mapping exists."""
    fcntl.fcntl(descriptor, F_ADD_SEALS, RECORD_SEALS)


def real_serving(settings: WorkerSettings) -> Serving:
    """The effects of a serving life on this machine, bounded by the settings."""
    return Serving(
        spawn=partial(
            spawn_gated, identity=ChildIdentity(*WIRE_IDENTITY, extra_groups=())
        ),
        gate_admits=partial(wire_gate_admits, identity=WIRE_IDENTITY),
        sweep=partial(
            sweep,
            SweepEffects(
                kill_all=partial(os.kill, -1, signal.SIGKILL),
                reap=reap_children,
                list_process_ids=partial(list_process_ids, Path("/proc")),
                clock=time.monotonic,
                sleep=time.sleep,
            ),
        ),
        create_record=create_request_record,
        seal=seal_record,
        watchdog=FaultHandlerWatchdog(),
        clock=time.monotonic,
        build_timeout_seconds=settings.build_timeout_seconds,
    )


def _serve_on_this_machine(
    *, controller_gid: int, runtime_uid: int, settings: WorkerSettings
) -> int:
    return serve(
        Path(SOCKET_DIRECTORY),
        controller_uid=0,
        controller_gid=controller_gid,
        runtime_uid=runtime_uid,
        serving=real_serving(settings),
    )


REAL_BOUNDARY = Boundary(
    _set_non_dumpable,
    _set_umask,
    partial(read_snapshot, Path("/")),
    load_worker_settings,
    _serve_on_this_machine,
)


def _refuse(refused: Predicate | StartRefusal) -> int:
    logger.error("start refused: %s", refused)
    return REFUSAL_EXIT_STATUS


def main(argv: Sequence[str] | None = None, boundary: Boundary = REAL_BOUNDARY) -> int:
    """Run the start-up steps; exit status 2 names a refusal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit after the self-check"
    )
    arguments = parser.parse_args(argv)
    try:
        boundary.set_non_dumpable()
    except OSError:
        return _refuse(Predicate.NON_DUMPABLE)
    boundary.set_umask()
    snapshot = boundary.read_snapshot()
    failing = first_failing_predicate(snapshot)
    if failing:
        return _refuse(failing)
    if arguments.check:
        return 0
    try:
        settings = boundary.load_settings()
    except ConfigurationError:
        logger.exception("settings refused")
        return _refuse(StartRefusal.SETTINGS)
    return boundary.serve(
        controller_gid=snapshot.identity.runtime_gid,
        runtime_uid=snapshot.identity.runtime_uid,
        settings=settings,
    )
