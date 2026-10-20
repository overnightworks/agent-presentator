"""The build worker refuses to start unless the container is sealed as designed."""

import os
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from presentator.host import worker
from presentator.host.config import (
    ConfigurationError,
    WorkerSettings,
    worker_settings_variables,
)
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
WORK_MOUNT, TEMPORARY_MOUNT, SHARED_MEMORY_MOUNT, *_ = worker.TMPFS_MOUNTS
HARDENED = "rw,nosuid,nodev,noexec"
SEALED_MOUNTINFO = "20 1 0:20 / / ro,relatime - overlay overlay rw\n" + "".join(
    f"{21 + index} 20 0:{21 + index} / {mount} {HARDENED} - tmpfs tmpfs rw\n"
    for index, mount in enumerate(worker.TMPFS_MOUNTS)
)
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
        with_mountinfo(
            f"{WORK_MOUNT} {HARDENED} - tmpfs", f"{WORK_MOUNT} {HARDENED} - ext4"
        ),
        Predicate.MOUNTS,
    ),
    "tmp without nosuid": (
        with_mountinfo(f"{TEMPORARY_MOUNT} rw,nosuid,", f"{TEMPORARY_MOUNT} rw,"),
        Predicate.MOUNTS,
    ),
    "work without noexec": (
        with_mountinfo(f"{WORK_MOUNT} {HARDENED}", f"{WORK_MOUNT} rw,nosuid,nodev"),
        Predicate.MOUNTS,
    ),
    "dev shm missing": (
        with_mountinfo(SHARED_MEMORY_MOUNT, "/dev/other"),
        Predicate.MOUNTS,
    ),
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
        with_toolchain(vite_link_target="/elsewhere/vite"),
        Predicate.TOOLCHAIN,
    ),
}


LOADED_SETTINGS = WorkerSettings(build_timeout_seconds=45.0)


def boundary_over(
    snapshot: Snapshot,
    events: list[str],
    served: dict[str, object] | None = None,
    load_settings: Callable[[], WorkerSettings] = lambda: LOADED_SETTINGS,
) -> Boundary:
    def read_snapshot() -> Snapshot:
        events.append("read")
        return snapshot

    def settings() -> WorkerSettings:
        events.append("settings")
        return load_settings()

    def serve(**arguments: object) -> int:
        events.append("serve")
        if served is not None:
            served.update(arguments)
        return 0

    return Boundary(
        set_non_dumpable=lambda: events.append("prctl"),
        set_umask=lambda: events.append("umask"),
        read_snapshot=read_snapshot,
        load_settings=settings,
        serve=serve,
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


def test_sealed_check_exits_zero_without_serving() -> None:
    events: list[str] = []

    assert worker.main(["--check"], boundary_over(sealed_snapshot(), events)) == 0
    assert "serve" not in events


def test_sealed_start_serves_and_returns_the_serving_status() -> None:
    events: list[str] = []

    assert worker.main([], boundary_over(sealed_snapshot(), events)) == 0
    assert events[-1] == "serve"


def test_start_serves_for_the_runtime_uid_and_the_controller_group() -> None:
    served: dict[str, object] = {}
    snapshot = with_identity(runtime_uid=1001)(sealed_snapshot())

    worker.main([], boundary_over(snapshot, [], served))

    assert served == {
        "controller_gid": 1000,
        "runtime_uid": 1001,
        "settings": LOADED_SETTINGS,
    }


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


def test_a_start_loads_the_settings_after_the_self_check_and_before_serving() -> None:
    events: list[str] = []

    worker.main([], boundary_over(sealed_snapshot(), events))

    assert events == ["prctl", "umask", "read", "settings", "serve"]


def test_a_refused_self_check_never_loads_the_settings() -> None:
    events: list[str] = []
    snapshot = with_fields(pid=7)(sealed_snapshot())

    assert (
        worker.main([], boundary_over(snapshot, events)) == worker.REFUSAL_EXIT_STATUS
    )
    assert "settings" not in events


def test_an_invalid_setting_refuses_by_variable_name_without_serving(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    def refusing() -> WorkerSettings:
        message = "PRESENTATOR_BUILD_TIMEOUT_SECONDS: not a number"
        raise ConfigurationError(message)

    status = worker.main([], boundary_over(sealed_snapshot(), events, None, refusing))

    assert status == worker.REFUSAL_EXIT_STATUS
    assert events[-1] == "settings"
    assert "PRESENTATOR_BUILD_TIMEOUT_SECONDS" in caplog.text
    assert worker.StartRefusal.SETTINGS in caplog.text


def test_the_settings_keys_pass_the_environment_predicate() -> None:
    snapshot = with_fields(environment_keys=worker_settings_variables())(
        sealed_snapshot()
    )

    assert worker.first_failing_predicate(snapshot) is None


def test_failing_prctl_refuses_before_reading_anything() -> None:
    events: list[str] = []

    def failing_prctl() -> None:
        raise OSError

    boundary = replace(
        boundary_over(sealed_snapshot(), events), set_non_dumpable=failing_prctl
    )

    assert worker.main(["--check"], boundary) == worker.REFUSAL_EXIT_STATUS
    assert events == []


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

    def read_only_root(_root: Path) -> SimpleNamespace:
        return SimpleNamespace(f_flag=os.ST_RDONLY)

    def dumpable_zero(_option: int, _value: int) -> int:
        return 0

    monkeypatch.setattr(worker.os, "statvfs", read_only_root)
    monkeypatch.setattr(worker, "libc_prctl", dumpable_zero)

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
    modules = tmp_path / "app/frontend/node_modules"
    file_at("app/frontend/node_modules/.bin/slidev", mode=0o755)
    package = modules / ".pnpm/@slidev+cli@1/node_modules/@slidev/cli"
    (package / "node_modules").mkdir(parents=True)
    (package / "node_modules/.vite").symlink_to(WORK_MOUNT + "/vite")
    (modules / "@slidev").mkdir()
    (modules / "@slidev/cli").symlink_to(package)
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
    assert snapshot.unreadable == frozenset()


def test_reads_a_missing_toolchain_as_absent(sealed_root: Path) -> None:
    (sealed_root / "app/frontend/node_modules/.bin/slidev").unlink()

    toolchain = worker.read_snapshot(sealed_root).toolchain

    assert toolchain.slidev_mode is None


def break_capability_bounding_set(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    status = SEALED_STATUS.replace("CapBnd:\t00000000000000e0", "CapBnd:\tzz")
    (root / "proc/self/status").write_text(status)


def remove_status(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "proc/self/status").unlink()


def remove_network_table(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "proc/net/dev").unlink()


def break_mountinfo(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "proc/self/mountinfo").write_text("garbage\n")


def break_mountinfo_encoding(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "proc/self/mountinfo").write_bytes(b"\xff\n")


def loop_slidev_package(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    link = root / "app/frontend/node_modules/@slidev/cli"
    link.unlink()
    link.symlink_to(link)


def deepen_slidev_package_chain(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    link = root / "app/frontend/node_modules/@slidev/cli"
    chain_length = sys.getrecursionlimit() + 100
    package = link.resolve()
    link.unlink()
    previous = package
    for hop in range(chain_length):
        hop_link = link.parent / f"hop-{hop}"
        hop_link.symlink_to(previous)
        previous = hop_link
    link.symlink_to(previous)


def break_slidev_package_link(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    link = root / "app/frontend/node_modules/@slidev/cli"
    package = link.resolve()
    link.unlink()
    link.symlink_to(root / "missing" / ".." / package.relative_to(root))


def grant_extra_capability(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    status = SEALED_STATUS.replace(
        "CapEff:\t00000000000000e0", "CapEff:\t00000000002000e0"
    )
    (root / "proc/self/status").write_text(status)


def grant_capability_and_break_mountinfo_encoding(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_extra_capability(root, monkeypatch)
    break_mountinfo_encoding(root, monkeypatch)


def grant_capability_and_loop_slidev_package(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_extra_capability(root, monkeypatch)
    loop_slidev_package(root, monkeypatch)


def grant_capability_and_deepen_slidev_package_chain(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_extra_capability(root, monkeypatch)
    deepen_slidev_package_chain(root, monkeypatch)


def grant_capability_and_break_slidev_package_link(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_extra_capability(root, monkeypatch)
    break_slidev_package_link(root, monkeypatch)


def remove_work(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "work").rmdir()


def fail_dumpable_read(_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_option: int, _value: int) -> int:
        raise OSError

    monkeypatch.setattr(worker, "libc_prctl", failing)


REFUSED_FACTS = {
    "capability bounding set not hexadecimal": (
        break_capability_bounding_set,
        Predicate.CAPABILITIES,
    ),
    "status file missing": (remove_status, Predicate.CAPABILITIES),
    "mountinfo malformed": (break_mountinfo, Predicate.MOUNTS),
    "work directory missing": (remove_work, Predicate.MOUNTS),
    "network table missing": (remove_network_table, Predicate.MOUNTS),
    "mountinfo not UTF-8": (break_mountinfo_encoding, Predicate.MOUNTS),
    "extra capability before non-UTF-8 mountinfo": (
        grant_capability_and_break_mountinfo_encoding,
        Predicate.CAPABILITIES,
    ),
    "extra capability before slidev package symlink loop": (
        grant_capability_and_loop_slidev_package,
        Predicate.CAPABILITIES,
    ),
    "extra capability before an over-deep slidev package chain": (
        grant_capability_and_deepen_slidev_package_chain,
        Predicate.CAPABILITIES,
    ),
    "extra capability before a broken slidev package link": (
        grant_capability_and_break_slidev_package_link,
        Predicate.CAPABILITIES,
    ),
    "dumpable unreadable": (fail_dumpable_read, Predicate.NON_DUMPABLE),
}


@pytest.mark.parametrize(
    ("arrange", "refused"), REFUSED_FACTS.values(), ids=REFUSED_FACTS.keys()
)
def test_unreadable_or_unparsable_fact_refuses_through_main_naming_its_predicate(
    sealed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    arrange: Callable[[Path, pytest.MonkeyPatch], None],
    refused: Predicate,
) -> None:
    arrange(sealed_root, monkeypatch)
    boundary = replace(
        boundary_over(sealed_snapshot(), []),
        read_snapshot=partial(worker.read_snapshot, sealed_root),
    )

    assert worker.main(["--check"], boundary) == worker.REFUSAL_EXIT_STATUS
    assert f"start refused: {refused}" in caplog.text


@pytest.fixture(params=["/run/docker.sock", "/var/run/docker.sock"])
def socket_path(request: pytest.FixtureRequest, sealed_root: Path) -> Path:
    path = sealed_root / request.param.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_socket_dangling(path: Path) -> None:
    path.symlink_to(path.parent / "nowhere")


def make_socket_present(path: Path) -> None:
    path.touch()


@pytest.mark.parametrize(
    "make_socket",
    [make_socket_present, make_socket_dangling],
    ids=["present", "dangling symlink"],
)
def test_reads_a_docker_socket_as_present(
    sealed_root: Path, socket_path: Path, make_socket: Callable[[Path], None]
) -> None:
    make_socket(socket_path)

    assert worker.read_snapshot(sealed_root).docker_socket_present is True


def test_denied_socket_lookup_marks_mounts_unreadable(
    sealed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path: Path) -> os.stat_result:
        raise PermissionError(path)

    monkeypatch.setattr(worker.os, "lstat", denied)

    assert worker.read_snapshot(sealed_root).unreadable == {Predicate.MOUNTS}


@pytest.mark.parametrize(
    "arrange",
    [loop_slidev_package, break_slidev_package_link, deepen_slidev_package_chain],
    ids=["loop", "broken link", "over-deep chain"],
)
def test_unresolvable_slidev_package_gives_no_vite_target(
    sealed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    arrange(sealed_root, monkeypatch)

    assert worker.read_snapshot(sealed_root).toolchain.vite_link_target is None


def unknown_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(worker.pwd, "getpwnam", refuse)


def test_unreadable_resource_bound_marks_its_predicate_unreadable(
    sealed_root: Path,
) -> None:
    (sealed_root / "sys/fs/cgroup/memory.max").unlink()

    unreadable = worker.read_snapshot(sealed_root).unreadable

    assert unreadable == {Predicate.RESOURCE_BOUNDS}


def test_unknown_user_marks_identities_unreadable(
    sealed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown_user(monkeypatch)

    assert worker.read_snapshot(sealed_root).unreadable == {Predicate.IDENTITIES}


def test_unreadable_later_fact_does_not_mask_an_earlier_failing_predicate(
    sealed_root: Path,
) -> None:
    (sealed_root / "proc/self/status").write_text(
        SEALED_STATUS.replace("00000000000000e0", "00000000002000e0", 1)
    )
    (sealed_root / "work").rmdir()

    snapshot = worker.read_snapshot(sealed_root)

    assert worker.first_failing_predicate(snapshot) == Predicate.CAPABILITIES
    assert snapshot.unreadable == {Predicate.MOUNTS}


def test_real_process_effects_make_it_non_dumpable_and_set_the_umask() -> None:
    previous_umask = os.umask(0)
    original_dumpable = worker.libc_prctl(worker.PR_GET_DUMPABLE, 0)
    try:
        worker.REAL_BOUNDARY.set_non_dumpable()
        worker.REAL_BOUNDARY.set_umask()
        assert worker.libc_prctl(worker.PR_GET_DUMPABLE, 0) == 0
        assert os.umask(previous_umask) == worker.ONLY_THE_OWNER
    finally:
        os.umask(previous_umask)
        worker.libc_prctl(worker.PR_SET_DUMPABLE, original_dumpable)


def test_prctl_failure_raises() -> None:
    with pytest.raises(OSError, match="prctl"):
        worker.libc_prctl(0x7FFFFFFF, 0)
