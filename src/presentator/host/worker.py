"""The build worker's start-up refusal: it runs only inside the sealed container."""

import argparse
import ctypes
import grp
import logging
import os
import pwd
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

REFUSAL_EXIT_STATUS = 2
WORKER_CAPABILITIES = 0xE0
MAXIMUM_PIDS = 512
RUNTIME_USER = "presentator"
BUILD_USER = "presentator-build"
WIRE_USER = "presentator-wire"
BUILD_IDENTITY = (10001, 10001)
WIRE_IDENTITY = (10002, 10002)
ROOT_MODE = 0o755
TMPFS_MOUNTS = ("/work", "/tmp", "/dev/shm", "/run/presentator-build")
NO_EXECUTION_MOUNTS = ("/work", "/tmp")
HARDENED_MOUNT_OPTIONS = frozenset({"nosuid", "nodev", "noexec"})
DOCKER_SOCKETS = ("/run/docker.sock", "/var/run/docker.sock")
SLIDEV_BINARY = "/app/frontend/node_modules/.bin/slidev"
COREPACK_DIRECTORY = "/opt/corepack"
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


@dataclass(frozen=True)
class Boundary:
    """The process effects and reads the start-up steps go through."""

    set_non_dumpable: Callable[[], None]
    set_umask: Callable[[], None]
    read_snapshot: Callable[[], Snapshot]


def _status_fields(process_status: str) -> dict[str, str]:
    fields = (line.partition(":") for line in process_status.splitlines())
    return {name: value.strip() for name, _, value in fields}


def _capabilities_sealed(snapshot: Snapshot) -> bool:
    fields = _status_fields(snapshot.process_status)
    worker = f"{WORKER_CAPABILITIES:016x}"
    return (
        fields.get("CapEff") == worker
        and fields.get("CapPrm") == worker
        and fields.get("CapInh") == "0" * 16
        and fields.get("CapAmb") == "0" * 16
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
            and not mode & (stat.S_IWGRP | stat.S_IWOTH)
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


def first_failing_predicate(snapshot: Snapshot) -> Predicate | None:
    """The first predicate, in refusal order, that the snapshot fails."""
    return next((name for name, holds in SELF_CHECK if not holds(snapshot)), None)


def _libc_prctl(option: int, value: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(option, value, 0, 0, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "prctl failed")
    return result


def _set_non_dumpable() -> None:
    _libc_prctl(PR_SET_DUMPABLE, 0)


def _set_umask() -> None:
    os.umask(0o022)


def _mode_if_file_or_directory(path: str) -> int | None:
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return None
    return mode if stat.S_ISREG(mode) or stat.S_ISDIR(mode) else None


def _vite_link_target(slidev_binary: str) -> str | None:
    package = Path(slidev_binary).resolve().parent.parent
    vite = package / "node_modules" / ".vite"
    return str(vite.readlink()) if vite.is_symlink() else None


def _read_toolchain() -> Toolchain:
    return Toolchain(
        slidev_mode=_mode_if_file_or_directory(SLIDEV_BINARY),
        corepack_mode=_mode_if_file_or_directory(COREPACK_DIRECTORY),
        headless_shell_modes=tuple(
            _mode_if_file_or_directory(str(match))
            for match in Path("/").glob(HEADLESS_SHELL_PATTERN)
        ),
        vite_link_target=_vite_link_target(SLIDEV_BINARY),
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


class UnreadableFactError(Exception):
    """A fact a predicate needs cannot be read, so that predicate cannot hold."""

    def __init__(self, predicate: Predicate) -> None:
        """Remember the predicate that cannot be proven."""
        super().__init__(predicate)
        self.predicate = predicate


def _read[Fact](predicate: Predicate, reading: Callable[[], Fact]) -> Fact:
    try:
        return reading()
    except (OSError, KeyError) as error:
        raise UnreadableFactError(predicate) from error


def _read_mount_facts() -> tuple[bool, str, os.stat_result, str]:
    return (
        bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        Path("/proc/self/mountinfo").read_text(),
        Path("/work").stat(),
        Path("/proc/net/dev").read_text(),
    )


def _read_resource_bounds() -> tuple[str, str]:
    return (
        Path("/sys/fs/cgroup/pids.max").read_text().strip(),
        Path("/sys/fs/cgroup/memory.max").read_text().strip(),
    )


def _read_snapshot() -> Snapshot:
    process_status = _read(Predicate.CAPABILITIES, Path("/proc/self/status").read_text)
    root_read_only, mountinfo, work, network_devices = _read(
        Predicate.MOUNTS, _read_mount_facts
    )
    pids_max, memory_max = _read(Predicate.RESOURCE_BOUNDS, _read_resource_bounds)
    return Snapshot(
        dumpable=_libc_prctl(PR_GET_DUMPABLE, 0),
        process_status=process_status,
        root_read_only=root_read_only,
        mountinfo=mountinfo,
        work_mode=stat.S_IMODE(work.st_mode),
        work_owner_uid=work.st_uid,
        network_devices=network_devices,
        docker_socket_present=any(Path(socket).exists() for socket in DOCKER_SOCKETS),
        pids_max=pids_max,
        memory_max=memory_max,
        identity=_read(Predicate.IDENTITIES, _read_identity),
        environment_keys=frozenset(os.environ),
        pid=os.getpid(),
        toolchain=_read_toolchain(),
    )


REAL_BOUNDARY = Boundary(_set_non_dumpable, _set_umask, _read_snapshot)


def _refuse(predicate: Predicate) -> int:
    logger.error("self-check refused: %s", predicate)
    return REFUSAL_EXIT_STATUS


def main(argv: Sequence[str] | None = None, boundary: Boundary = REAL_BOUNDARY) -> int:
    """Run start-up steps 1 to 3; exit status 2 names a refusal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit after the self-check"
    )
    parser.parse_args(argv)
    try:
        boundary.set_non_dumpable()
    except OSError:
        return _refuse(Predicate.NON_DUMPABLE)
    boundary.set_umask()
    try:
        failing = first_failing_predicate(boundary.read_snapshot())
    except UnreadableFactError as unreadable:
        return _refuse(unreadable.predicate)
    return _refuse(failing) if failing else 0


if __name__ == "__main__":
    sys.exit(main())
