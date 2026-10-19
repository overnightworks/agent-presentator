"""The build worker refuses to start unless the container is sealed as designed."""

import os
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from presentator.host import worker
from presentator.host.worker import (
    Boundary,
    Identity,
    Predicate,
    Snapshot,
    Toolchain,
)

SEALED_STATUS = """Name:\tpresentator-worker
CapInh:\t0000000000000000
CapPrm:\t00000000000000e0
CapEff:\t00000000000000e0
CapBnd:\t00000000000000e0
CapAmb:\t0000000000000000
NoNewPrivs:\t1
Seccomp:\t2
"""
SEALED_MOUNTINFO = """\
20 1 0:20 / / ro,relatime - overlay overlay rw
21 20 0:21 / /work rw,nosuid,nodev,noexec - tmpfs tmpfs rw
22 20 0:22 / /tmp rw,nosuid,nodev,noexec - tmpfs tmpfs rw
23 20 0:23 / /dev/shm rw,nosuid,nodev,noexec - tmpfs shm rw
24 20 0:24 / /run/presentator-build rw,nosuid,nodev,noexec - tmpfs tmpfs rw
"""
NETWORK_HEADER = "Inter-|   Receive\n face |bytes\n"
LOOPBACK_ONLY = NETWORK_HEADER + "    lo: 0 0\n"
WORLD_READABLE_EXECUTABLE = 0o755


def sealed_snapshot() -> Snapshot:
    return Snapshot(
        dumpable=0,
        process_status=SEALED_STATUS,
        root_read_only=True,
        mountinfo=SEALED_MOUNTINFO,
        work_mode=0o755,
        work_owner_uid=0,
        network_devices=LOOPBACK_ONLY,
        docker_socket_present=False,
        pids_max="512",
        memory_max="1073741824",
        identity=Identity(
            uid=0,
            euid=0,
            gid=1000,
            supplementary_gids=(1000,),
            runtime_uid=1000,
            runtime_gid=1000,
            users={
                "presentator-build": (10001, 10001),
                "presentator-wire": (10002, 10002),
            },
        ),
        environment_keys=frozenset({"PATH", "HOME", "HOSTNAME"}),
        pid=1,
        toolchain=Toolchain(
            slidev_mode=stat.S_IFREG | WORLD_READABLE_EXECUTABLE,
            corepack_mode=stat.S_IFDIR | WORLD_READABLE_EXECUTABLE,
            headless_shell_modes=(stat.S_IFREG | WORLD_READABLE_EXECUTABLE,),
            vite_link_target="/work/vite",
        ),
    )


def with_status(**changes: str) -> Callable[[Snapshot], Snapshot]:
    def change(snapshot: Snapshot) -> Snapshot:
        lines = snapshot.process_status.splitlines()
        kept = [line for line in lines if line.partition(":")[0] not in changes]
        edited = [*kept, *(f"{name}:\t{value}" for name, value in changes.items())]
        return replace(snapshot, process_status="\n".join(edited))

    return change


def with_identity(**changes: object) -> Callable[[Snapshot], Snapshot]:
    return lambda snapshot: replace(
        snapshot, identity=replace(snapshot.identity, **changes)
    )


def with_users(**users: tuple[int, int]) -> Callable[[Snapshot], Snapshot]:
    return lambda snapshot: with_identity(users={**snapshot.identity.users, **users})(
        snapshot
    )


def with_toolchain(**changes: object) -> Callable[[Snapshot], Snapshot]:
    return lambda snapshot: replace(
        snapshot, toolchain=replace(snapshot.toolchain, **changes)
    )


def with_mountinfo(old: str, new: str) -> Callable[[Snapshot], Snapshot]:
    return lambda snapshot: replace(
        snapshot, mountinfo=snapshot.mountinfo.replace(old, new)
    )


def with_fields(**changes: object) -> Callable[[Snapshot], Snapshot]:
    return lambda snapshot: replace(snapshot, **changes)


GROUP_WRITABLE = stat.S_IFREG | 0o775
FAILURES = {
    "dumpable": (with_fields(dumpable=1), Predicate.NON_DUMPABLE),
    "CAP_SYS_ADMIN effective": (
        with_status(CapEff="00000000002000e0"),
        Predicate.CAPABILITIES,
    ),
    "permitted capability missing": (
        with_status(CapPrm="0000000000000060"),
        Predicate.CAPABILITIES,
    ),
    "inheritable capability": (
        with_status(CapInh="0000000000000020"),
        Predicate.CAPABILITIES,
    ),
    "ambient capability": (
        with_status(CapAmb="0000000000000020"),
        Predicate.CAPABILITIES,
    ),
    "wider bounding set": (
        with_status(CapBnd="00000000000000ff"),
        Predicate.CAPABILITIES,
    ),
    "new privileges allowed": (with_status(NoNewPrivs="0"), Predicate.CAPABILITIES),
    "no seccomp filter": (with_status(Seccomp="0"), Predicate.CAPABILITIES),
    "writable root": (with_fields(root_read_only=False), Predicate.MOUNTS),
    "work not tmpfs": (
        with_mountinfo("- tmpfs tmpfs rw\n22", "- ext4 tmpfs rw\n22"),
        Predicate.MOUNTS,
    ),
    "tmp without nosuid": (
        with_mountinfo("/tmp rw,nosuid,", "/tmp rw,"),
        Predicate.MOUNTS,
    ),
    "work without noexec": (
        with_mountinfo("/work rw,nosuid,nodev,noexec", "/work rw,nosuid,nodev"),
        Predicate.MOUNTS,
    ),
    "dev shm missing": (with_mountinfo("/dev/shm", "/dev/other"), Predicate.MOUNTS),
    "work sticky world-writable": (with_fields(work_mode=0o1777), Predicate.MOUNTS),
    "work not root": (with_fields(work_owner_uid=1000), Predicate.MOUNTS),
    "second interface": (
        with_fields(network_devices=LOOPBACK_ONLY + "  eth0: 0 0\n"),
        Predicate.MOUNTS,
    ),
    "docker socket present": (
        with_fields(docker_socket_present=True),
        Predicate.MOUNTS,
    ),
    "pids unlimited": (with_fields(pids_max="max"), Predicate.RESOURCE_BOUNDS),
    "pids above bound": (with_fields(pids_max="513"), Predicate.RESOURCE_BOUNDS),
    "memory unlimited": (with_fields(memory_max="max"), Predicate.RESOURCE_BOUNDS),
    "build uid root": (
        with_users(**{"presentator-build": (0, 0)}),
        Predicate.IDENTITIES,
    ),
    "build uid is runtime uid": (
        with_identity(runtime_uid=10001),
        Predicate.IDENTITIES,
    ),
    "wire uid root": (with_users(**{"presentator-wire": (0, 0)}), Predicate.IDENTITIES),
    "wire uid is runtime uid": (with_identity(runtime_uid=10002), Predicate.IDENTITIES),
    "wire uid is build uid": (
        with_users(**{"presentator-wire": (10001, 10001)}),
        Predicate.IDENTITIES,
    ),
    "wrong controller gid": (with_identity(gid=0), Predicate.IDENTITIES),
    "extra supplementary group": (
        with_identity(supplementary_gids=(1000, 4)),
        Predicate.IDENTITIES,
    ),
    "not root": (with_identity(uid=1000, euid=1000), Predicate.IDENTITIES),
    "secret key in environment": (
        with_fields(environment_keys=frozenset({"PATH", "PRESENTATOR_SECRET_KEY"})),
        Predicate.ENVIRONMENT,
    ),
    "not pid one": (with_fields(pid=7), Predicate.PID_ONE),
    "slidev missing": (with_toolchain(slidev_mode=None), Predicate.TOOLCHAIN),
    "corepack missing": (with_toolchain(corepack_mode=None), Predicate.TOOLCHAIN),
    "headless shell missing": (
        with_toolchain(headless_shell_modes=()),
        Predicate.TOOLCHAIN,
    ),
    "headless shell matched twice": (
        with_toolchain(headless_shell_modes=(GROUP_WRITABLE & ~0o020,) * 2),
        Predicate.TOOLCHAIN,
    ),
    "headless shell group-writable": (
        with_toolchain(headless_shell_modes=(GROUP_WRITABLE,)),
        Predicate.TOOLCHAIN,
    ),
    "toolchain not executable by others": (
        with_toolchain(slidev_mode=stat.S_IFREG | 0o750),
        Predicate.TOOLCHAIN,
    ),
    "vite cache a directory": (
        with_toolchain(vite_link_target=None),
        Predicate.TOOLCHAIN,
    ),
    "vite cache linked elsewhere": (
        with_toolchain(vite_link_target="/tmp/vite"),
        Predicate.TOOLCHAIN,
    ),
}


def boundary_over(snapshot: Snapshot, events: list[str]) -> Boundary:
    def read_snapshot() -> Snapshot:
        events.append("read")
        return snapshot

    return Boundary(
        set_non_dumpable=lambda: events.append("prctl"),
        set_umask=lambda: events.append("umask"),
        read_snapshot=read_snapshot,
    )


def test_sealed_container_passes_the_self_check() -> None:
    assert worker.first_failing_predicate(sealed_snapshot()) is None


@pytest.mark.parametrize(("change", "refused"), FAILURES.values(), ids=FAILURES.keys())
def test_self_check_names_the_failing_predicate(
    change: Callable[[Snapshot], Snapshot], refused: Predicate
) -> None:
    assert worker.first_failing_predicate(change(sealed_snapshot())) == refused


def test_double_fault_names_the_first_predicate_in_refusal_order() -> None:
    snapshot = with_identity(gid=0)(with_status(Seccomp="0")(sealed_snapshot()))

    assert worker.first_failing_predicate(snapshot) == Predicate.CAPABILITIES


@pytest.mark.parametrize("arguments", [[], ["--check"]])
def test_sealed_start_exits_zero(arguments: list[str]) -> None:
    assert worker.main(arguments, boundary_over(sealed_snapshot(), [])) == 0


def test_refusal_exits_two_and_logs_the_predicate_without_environment_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot = with_fields(
        pid=7, environment_keys=frozenset({"PRESENTATOR_SECRET_KEY"})
    )(sealed_snapshot())

    assert (
        worker.main(["--check"], boundary_over(snapshot, []))
        == worker.REFUSAL_EXIT_STATUS
    )
    assert Predicate.ENVIRONMENT in caplog.text
    assert "PRESENTATOR_SECRET_KEY" not in caplog.text


def test_start_up_makes_the_process_non_dumpable_before_reading_anything() -> None:
    events: list[str] = []

    worker.main(["--check"], boundary_over(sealed_snapshot(), events))

    assert events == ["prctl", "umask", "read"]


def test_failing_prctl_refuses_before_reading_anything() -> None:
    events: list[str] = []

    def failing_prctl() -> None:
        raise OSError

    boundary = replace(
        boundary_over(sealed_snapshot(), events), set_non_dumpable=failing_prctl
    )

    assert worker.main(["--check"], boundary) == worker.REFUSAL_EXIT_STATUS
    assert events == []


def test_unreadable_fact_refuses_naming_its_predicate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unreadable_snapshot() -> Snapshot:
        raise worker.UnreadableFactError(Predicate.RESOURCE_BOUNDS)

    boundary = replace(
        boundary_over(sealed_snapshot(), []), read_snapshot=unreadable_snapshot
    )

    assert worker.main(["--check"], boundary) == worker.REFUSAL_EXIT_STATUS
    assert Predicate.RESOURCE_BOUNDS in caplog.text


def stand_in_users(monkeypatch: pytest.MonkeyPatch) -> None:
    users = {
        "presentator": (1000, 1000),
        "presentator-build": (10001, 10001),
        "presentator-wire": (10002, 10002),
    }

    def password_entry(name: str) -> SimpleNamespace:
        uid, gid = users[name]
        return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid)

    def group_entry(name: str) -> SimpleNamespace:
        return SimpleNamespace(gr_gid=users[name][1])

    monkeypatch.setattr(worker.pwd, "getpwnam", password_entry)
    monkeypatch.setattr(worker.grp, "getgrnam", group_entry)


@pytest.fixture
def sealed_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    stand_in_users(monkeypatch)

    def file_at(relative: str, content: str = "", mode: int = 0o644) -> Path:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    file_at("proc/self/status", SEALED_STATUS)
    file_at("proc/self/mountinfo", SEALED_MOUNTINFO)
    file_at("proc/net/dev", LOOPBACK_ONLY)
    file_at("sys/fs/cgroup/pids.max", "512\n")
    file_at("sys/fs/cgroup/memory.max", "1073741824\n")
    (tmp_path / "work").mkdir()
    (tmp_path / "work").chmod(0o755)
    (tmp_path / "opt/corepack").mkdir(parents=True)
    (tmp_path / "opt/corepack").chmod(0o755)
    file_at("app/frontend/node_modules/.bin/slidev", mode=0o755)
    (tmp_path / "app/frontend/node_modules/node_modules").mkdir()
    (tmp_path / "app/frontend/node_modules/node_modules/.vite").symlink_to("/work/vite")
    shell = (
        "opt/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64"
    )
    file_at(f"{shell}/chrome-headless-shell", mode=0o755)
    return tmp_path


def test_reads_the_sealed_facts_from_the_filesystem(sealed_root: Path) -> None:
    snapshot = worker.read_snapshot(sealed_root)

    assert snapshot.process_status == SEALED_STATUS
    assert snapshot.network_devices == LOOPBACK_ONLY
    assert (snapshot.pids_max, snapshot.memory_max) == ("512", "1073741824")
    assert (snapshot.work_mode, snapshot.work_owner_uid) == (
        0o755,
        sealed_root.stat().st_uid,
    )
    assert not snapshot.docker_socket_present
    assert snapshot.identity.users["presentator-wire"] == (10002, 10002)
    assert snapshot.toolchain.vite_link_target == "/work/vite"
    assert len(snapshot.toolchain.headless_shell_modes) == 1
    assert snapshot.toolchain.corepack_mode is not None


def test_reads_a_present_docker_socket(sealed_root: Path) -> None:
    (sealed_root / "run").mkdir()
    (sealed_root / "run/docker.sock").touch()

    assert worker.read_snapshot(sealed_root).docker_socket_present


def test_reads_a_missing_toolchain_as_absent(sealed_root: Path) -> None:
    (sealed_root / "app/frontend/node_modules/.bin/slidev").unlink()

    toolchain = worker.read_snapshot(sealed_root).toolchain

    assert toolchain.slidev_mode is None


@pytest.mark.parametrize(
    ("missing", "refused"),
    [
        ("proc/self/status", Predicate.CAPABILITIES),
        ("proc/net/dev", Predicate.MOUNTS),
        ("sys/fs/cgroup/memory.max", Predicate.RESOURCE_BOUNDS),
    ],
)
def test_unreadable_file_names_its_predicate(
    sealed_root: Path, missing: str, refused: Predicate
) -> None:
    (sealed_root / missing).unlink()

    with pytest.raises(worker.UnreadableFactError) as raised:
        worker.read_snapshot(sealed_root)

    assert raised.value.predicate == refused


def test_unknown_user_makes_identities_unreadable(
    sealed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown_user(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(worker.pwd, "getpwnam", unknown_user)

    with pytest.raises(worker.UnreadableFactError) as raised:
        worker.read_snapshot(sealed_root)

    assert raised.value.predicate == Predicate.IDENTITIES


def test_real_process_effects_make_it_non_dumpable_and_set_the_umask() -> None:
    previous_umask = os.umask(0)
    try:
        worker.REAL_BOUNDARY.set_non_dumpable()
        worker.REAL_BOUNDARY.set_umask()
        assert os.umask(previous_umask) == worker.GROUP_AND_OTHERS_WRITE
    finally:
        os.umask(previous_umask)
        worker.libc_prctl(worker.PR_SET_DUMPABLE, 1)


def test_prctl_failure_raises() -> None:
    with pytest.raises(OSError, match="prctl"):
        worker.libc_prctl(0x7FFFFFFF, 0)
