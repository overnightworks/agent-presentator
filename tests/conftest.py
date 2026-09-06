"""A real git remote in a temporary directory, for every layer that reads one."""

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

EXAMPLE_DECK = Path(__file__).parents[1] / "examples" / "hello-deck"
EXAMPLE_SLUG = EXAMPLE_DECK.name
EXAMPLE_TITLE = "Hello Co-Presenter"
MAIN_BRANCH = "main"

_AUTHORSHIP = {
    "GIT_AUTHOR_NAME": "a test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "a test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(inside: Path, *arguments: str, at: datetime | None = None) -> None:
    stamped = {} if at is None else {"GIT_COMMITTER_DATE": at.isoformat()}
    subprocess.run(
        ["git", *arguments],
        capture_output=True,
        check=True,
        cwd=inside,
        env={**os.environ, **_AUTHORSHIP, **stamped},
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class GitRemote:
    """A bare repository a mirror can pull from, and the tree that fills it."""

    bare: Path
    work: Path

    @property
    def url(self) -> str:
        """The address a source is configured with."""
        return self.bare.as_uri()

    def commit(self, files: Mapping[str, str], *, at: datetime) -> None:
        """Write the files, commit them at that moment, and push."""
        for path, content in files.items():
            written = self.work / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(content, encoding="utf-8")
        self._push(at=at)

    def commit_example_deck(self, *, at: datetime, into: str = EXAMPLE_SLUG) -> None:
        """Copy the repository's own example deck in and push it."""
        shutil.copytree(EXAMPLE_DECK, self.work / into, dirs_exist_ok=True)
        self._push(at=at)

    def _push(self, *, at: datetime) -> None:
        _git(self.work, "add", "--all")
        _git(self.work, "commit", "--quiet", "--message", "a push", at=at)
        _git(self.work, "push", "--quiet", "origin", MAIN_BRANCH)


@pytest.fixture
def remote(tmp_path: Path) -> GitRemote:
    """An empty bare repository with a working tree that pushes into it."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(tmp_path, "init", "--bare", "--quiet", str(bare))
    _git(work, "init", "--quiet", "--initial-branch", MAIN_BRANCH)
    _git(work, "remote", "add", "origin", str(bare))
    return GitRemote(bare=bare, work=work)
