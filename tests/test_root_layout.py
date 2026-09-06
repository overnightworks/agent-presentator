"""Root layout allowlist check."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CHECK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_root_layout.py"
_OFFENDER_LINE = (
    "{entry}: not allowed at the repository root; move it next to its owner\n"
)


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        message = "git is required to prove the root layout check"
        raise FileNotFoundError(message)
    return git


def _run_git(repository: Path, *args: str) -> None:
    subprocess.run(
        [_git_executable(), *args],
        check=True,
        cwd=repository,
        capture_output=True,
        text=True,
    )


def _write_tracked_readme(repository: Path) -> None:
    (repository / "README.md").write_text("allowed\n", encoding="utf-8")
    _run_git(repository, "add", "README.md")


@pytest.mark.parametrize(
    ("foreign_root_entry", "expected_returncode", "expected_stdout"),
    [
        pytest.param(None, 0, "", id="allowlisted-root-passes"),
        pytest.param(
            "misc",
            1,
            _OFFENDER_LINE.format(entry="misc"),
            id="foreign-root-entry-fails",
        ),
    ],
)
def test_root_layout_check_reports_whether_every_root_entry_is_allowed(
    tmp_path: Path,
    foreign_root_entry: str | None,
    expected_returncode: int,
    expected_stdout: str,
) -> None:
    _run_git(tmp_path, "init")
    _write_tracked_readme(tmp_path)
    if foreign_root_entry is not None:
        foreign_file = tmp_path / foreign_root_entry / "x.txt"
        foreign_file.parent.mkdir()
        foreign_file.write_text("disallowed\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT)],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == expected_returncode
    assert completed.stdout == expected_stdout
