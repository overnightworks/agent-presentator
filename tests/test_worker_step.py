"""The step's launcher and the writable paths the controller makes for it."""

import errno
import fcntl
import os
import runpy
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from presentator.adapters.build_wire import StepHeader, write_record
from presentator.contracts.decks import StepKind
from presentator.host import worker, worker_step
from presentator.host.worker import create_request_record
from tests.worker_support import open_descriptor_count

CONTROLLER_UMASK = 0o077
OPEN_DIRECTORY_MODE = 0o1777
STEP_ERROR_MODE = 0o644
NOISE = b"\x00\xff\x10 noise"


def build_arguments(work: Path) -> list[str]:
    return [
        "pnpm",
        "exec",
        "slidev",
        "build",
        f"{work}/deck/slides.md",
        "--base",
        "/deck/my-deck/",
        "--out",
        f"{work}/out/talk",
    ]


def export_arguments(work: Path) -> list[str]:
    return [
        "pnpm",
        "exec",
        "slidev",
        "export",
        f"{work}/deck/slides.md",
        "--output",
        f"{work}/out/deck.pdf",
    ]


EXECUTIONS: dict[str, tuple[StepKind, Callable[[Path], list[str]]]] = {
    "a build": (StepKind.BUILD, build_arguments),
    "an export": (StepKind.EXPORT, export_arguments),
}


@dataclass
class Executions:
    """Stands in for the exec: it remembers what it was asked and what was open."""

    record: int
    calls: list[tuple[str, list[str]]] = field(
        default_factory=list[tuple[str, list[str]]]
    )
    record_was_open: list[bool] = field(default_factory=list[bool])

    def __call__(self, program: str, arguments: list[str]) -> None:
        self.calls.append((program, arguments))
        self.record_was_open.append(is_open(self.record))


def is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


@pytest.fixture
def record() -> Iterator[int]:
    descriptor = create_request_record()
    try:
        yield descriptor
    finally:
        if is_open(descriptor):
            os.close(descriptor)


def arguments_of(record: int, work: Path) -> list[str]:
    return ["--record-fd", str(record), "--work", str(work)]


@pytest.mark.parametrize(
    ("kind", "expected"), EXECUTIONS.values(), ids=EXECUTIONS.keys()
)
def test_the_launcher_execs_slidev_with_the_arguments_of_the_sealed_record(
    record: int,
    tmp_path: Path,
    kind: StepKind,
    expected: Callable[[Path], list[str]],
) -> None:
    write_record(record, StepHeader(step=kind, slug="my-deck"))
    execute = Executions(record)
    working_directory = Path.cwd()

    exit_status = worker_step.main(arguments_of(record, tmp_path), execute)

    assert exit_status == 0
    assert execute.calls == [("pnpm", expected(tmp_path))]
    assert execute.record_was_open == [False]
    assert Path.cwd() == working_directory


def test_the_launcher_exits_non_zero_without_exec_for_a_refused_record(
    record: int, tmp_path: Path
) -> None:
    os.pwrite(record, NOISE, 0)
    execute = Executions(record)

    exit_status = worker_step.main(arguments_of(record, tmp_path), execute)

    assert exit_status == worker_step.RECORD_REFUSED_EXIT_STATUS
    assert execute.calls == []
    assert not is_open(record)


def test_run_as_a_program_the_launcher_exits_with_the_refusal_status(
    monkeypatch: pytest.MonkeyPatch, record: int, tmp_path: Path
) -> None:
    os.pwrite(record, NOISE, 0)
    monkeypatch.setattr(sys, "argv", ["worker_step", *arguments_of(record, tmp_path)])
    monkeypatch.delitem(sys.modules, "presentator.host.worker_step")

    with pytest.raises(SystemExit) as exited:
        runpy.run_module("presentator.host.worker_step", run_name="__main__")

    assert exited.value.code == worker_step.RECORD_REFUSED_EXIT_STATUS


@pytest.fixture
def work(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "work"
    root.mkdir()
    (root / "deck").mkdir(mode=0o755)
    previous = os.umask(CONTROLLER_UMASK)
    try:
        yield root
    finally:
        os.umask(previous)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot(path: Path) -> tuple[int, list[str] | str]:
    """The mode of a path and what it holds: its entries or its text."""
    held = sorted(entry.name for entry in path.iterdir()) if path.is_dir() else None
    return mode_of(path), held if held is not None else path.read_text()


def test_the_step_paths_are_open_directories_and_an_empty_append_only_step_err(
    work: Path,
) -> None:
    descriptor = worker.prepare_step_paths(work)
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        close_on_exec = fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        os.close(descriptor)

    for directory in ("deck/node_modules", "home", "out", "vite"):
        assert (work / directory).is_dir()
        assert mode_of(work / directory) == OPEN_DIRECTORY_MODE
    step_error = work / "step.err"
    assert stat.S_ISREG(step_error.stat().st_mode)
    assert mode_of(step_error) == STEP_ERROR_MODE
    assert step_error.read_bytes() == b""
    assert flags & os.O_ACCMODE == os.O_WRONLY
    assert flags & os.O_APPEND
    assert close_on_exec


def test_a_step_err_that_cannot_be_given_its_mode_is_closed_and_fails_the_paths(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_descriptor: int, _mode: int) -> None:
        raise OSError(errno.EPERM, "mode refused")

    monkeypatch.setattr(os, "fchmod", refuse)
    before = open_descriptor_count()

    with pytest.raises(OSError, match="mode refused"):
        worker.prepare_step_paths(work)

    assert open_descriptor_count() == before


def an_existing_node_modules(work: Path, _outside: Path) -> None:
    (work / "deck" / "node_modules").mkdir()


def a_node_modules_link(work: Path, outside: Path) -> None:
    (work / "deck" / "node_modules").symlink_to(outside)


def an_output_file(work: Path, _outside: Path) -> None:
    (work / "out").write_text("in the way")


def a_step_err_link(work: Path, outside: Path) -> None:
    (work / "step.err").symlink_to(outside)


OCCUPIED_PATHS = {
    "an existing node_modules directory": (an_existing_node_modules, "directory"),
    "a node_modules link to an outside directory": (a_node_modules_link, "directory"),
    "an out file": (an_output_file, "directory"),
    "a step.err link to an outside file": (a_step_err_link, "file"),
}


@pytest.mark.parametrize(
    ("occupy", "outside_kind"), OCCUPIED_PATHS.values(), ids=OCCUPIED_PATHS.keys()
)
def test_an_existing_entry_fails_the_paths_and_leaves_what_it_points_at_alone(
    work: Path,
    tmp_path: Path,
    occupy: Callable[[Path, Path], None],
    outside_kind: str,
) -> None:
    outside = tmp_path / "outside"
    if outside_kind == "directory":
        outside.mkdir(mode=0o700)
    else:
        outside.write_text("kept")
        outside.chmod(0o600)
    occupy(work, outside)
    before = snapshot(outside)

    with pytest.raises(OSError, match="exists"):
        worker.prepare_step_paths(work)

    assert snapshot(outside) == before
