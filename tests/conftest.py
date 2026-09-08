"""A real git remote in a temporary directory, for every layer that reads one."""

import os
import shutil
import stat
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


def a_path_carrying(*directories: Path) -> str:
    """A PATH with git on it, because reading a deck is still spawning git."""
    git = shutil.which("git")
    assert git is not None
    beside_git = Path(git).parent
    return os.pathsep.join(str(directory) for directory in (*directories, beside_git))


def with_the_program(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    program: str,
    script: str,
) -> None:
    """Put a program this test wrote on PATH, ahead of any real one."""
    somewhere_on_path = tmp_path / "programs-on-path"
    somewhere_on_path.mkdir(exist_ok=True)
    stand_in = somewhere_on_path / program
    stand_in.write_text(script, encoding="utf-8")
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IEXEC)
    machine.setenv("PATH", a_path_carrying(somewhere_on_path))


def _git(inside: Path, *arguments: str, at: datetime | None = None) -> str:
    stamped = {} if at is None else {"GIT_COMMITTER_DATE": at.isoformat()}
    ran = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        check=True,
        cwd=inside,
        env={**os.environ, **_AUTHORSHIP, **stamped},
    )
    return ran.stdout.decode().strip()


@dataclass(frozen=True, slots=True, kw_only=True)
class GitRemote:
    """A bare repository a mirror can pull from, and the tree that fills it."""

    bare: Path
    work: Path

    @property
    def url(self) -> str:
        """The address a source is configured with."""
        return self.bare.as_uri()

    @property
    def head(self) -> str:
        """The commit this remote's newest push wrote."""
        return _git(self.work, "rev-parse", "HEAD")

    def commit(self, files: Mapping[str, str], *, at: datetime) -> None:
        """Write the files, commit them at that moment, and push."""
        for path, content in files.items():
            written = self.work / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(content, encoding="utf-8")
        self._push(at=at)

    def remove(self, folder: str, *, at: datetime) -> None:
        """Delete the folder from the tree, commit that deletion, and push."""
        shutil.rmtree(self.work / folder)
        self._push(at=at)

    def commit_example_deck(self, *, at: datetime, into: str = EXAMPLE_SLUG) -> None:
        """Copy the repository's own example deck in and push it."""
        shutil.copytree(EXAMPLE_DECK, self.work / into, dirs_exist_ok=True)
        self._push(at=at)

    def _push(self, *, at: datetime) -> None:
        _git(self.work, "add", "--all")
        _git(self.work, "commit", "--quiet", "--message", "a push", at=at)
        _git(self.work, "push", "--quiet", "origin", MAIN_BRANCH)


def _a_remote(under: Path, named: str) -> GitRemote:
    bare = under / f"{named}.git"
    work = under / f"{named}-work"
    work.mkdir()
    _git(under, "init", "--bare", "--quiet", str(bare))
    _git(work, "init", "--quiet", "--initial-branch", MAIN_BRANCH)
    _git(work, "remote", "add", "origin", str(bare))
    return GitRemote(bare=bare, work=work)


@pytest.fixture
def remote(tmp_path: Path) -> GitRemote:
    """An empty bare repository with a working tree that pushes into it."""
    return _a_remote(tmp_path, "remote")


@pytest.fixture
def another_remote(tmp_path: Path) -> GitRemote:
    """A second one, for what two sources of one instance do to one another."""
    return _a_remote(tmp_path, "another-remote")
