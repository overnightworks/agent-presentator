"""The root watcher measures watch ends at the boundary and at the stdin close."""

import ctypes
import importlib
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def watcher(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(SCRIPTS / "probe_build_worker"), *sys.path])
    return importlib.import_module("upload_watcher")


def test_a_watch_that_ends_after_the_boundary_is_read_until_the_stdin_close(
    watcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defect:#463 - the watch end between boundary and stdin close is measured."""
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.inotify_init1(os.O_CLOEXEC)
    watch = libc.inotify_add_watch(
        descriptor, str(tmp_path).encode(), watcher.IN_CREATE | watcher.IN_ONLYDIR
    )
    read_end, write_end = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: read_end))
    (tmp_path / watcher.BOUNDARY_NAME).touch()
    libc.inotify_rm_watch(descriptor, watch)
    os.close(write_end)

    report = watcher.record(descriptor, {watch: watcher.BOUNDARY_ROOT})

    os.close(read_end)
    os.close(descriptor)
    assert report["ended_at_boundary"] == []
    assert report["ended_at_stdin_close"] == [watcher.BOUNDARY_ROOT]
