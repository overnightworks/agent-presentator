"""Allowlist the repository root.

Owner: marketplace AGENTS.md section "Repository layout". The root holds only
what a tool must find there and the entry documents; everything else lives next
to its owner. This script is a CI gate, not a package layer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ALLOWED_ROOT_FILES: frozenset[str] = frozenset(
    {
        ".dockerignore",
        ".gitattributes",
        ".gitignore",
        ".python-version",
        "AGENTS.md",
        "CLAUDE.md",
        "Dockerfile",
        "LICENSE",
        "README.md",
        "compose.yaml",
        "pyproject.toml",
        "sonar-project.properties",
        "uv.lock",
    }
)
ALLOWED_ROOT_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".agent-claim",
        ".github",
        "copresenter",
        "docs",
        "examples",
        "frontend",
        "scripts",
        "speech",
        "src",
        "tests",
    }
)

_OFFENDER_MESSAGE = (
    "{entry}: not allowed at the repository root; move it next to its owner"
)


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        message = "git is required to check the repository root layout"
        raise FileNotFoundError(message)
    return git


def _git_ls_files(repository: Path, *args: str) -> tuple[str, ...]:
    completed = subprocess.run(
        [_git_executable(), "ls-files", *args],
        check=True,
        capture_output=True,
        cwd=repository,
        text=True,
    )
    return tuple(path for path in completed.stdout.splitlines() if path)


def _root_entry(path: str) -> str:
    return Path(path).parts[0]


def disallowed_root_entries(repository: Path) -> tuple[str, ...]:
    """Return sorted root names that are neither an allowlisted file nor directory."""
    allowed = ALLOWED_ROOT_FILES | ALLOWED_ROOT_DIRECTORIES
    entries = {
        _root_entry(path)
        for path in (
            *_git_ls_files(repository),
            *_git_ls_files(repository, "--others", "--exclude-standard"),
        )
    }
    return tuple(sorted(entries - allowed))


def main() -> int:
    """Print each disallowed root entry and return 1 when any exist."""
    offenders = disallowed_root_entries(Path.cwd())
    if not offenders:
        return 0
    for entry in offenders:
        sys.stdout.write(f"{_OFFENDER_MESSAGE.format(entry=entry)}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
