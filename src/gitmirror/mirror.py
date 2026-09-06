"""Keeps a bare mirror of one source by driving the `git` executable.

git's own program already owns the transports, the credential helpers, and the
prompt suppression an unattended server needs, so this package spawns it rather
than binding a second git implementation (ADR 0010).
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from gitmirror.model import (
    Connection,
    ConnectionState,
    CredentialResolver,
    GitSource,
    GitUnavailableError,
    MirrorError,
    Revision,
    TreeEntry,
)

_CREDENTIAL_VARIABLE: Final = "GITMIRROR_CREDENTIAL"
# git asks a helper for the password of an https remote, and answering out of
# the child's environment keeps the secret off the command line and off disk.
# The user name belongs in the source URL: only the operator knows which name
# the host expects beside a read-only token.
_CREDENTIAL_HELPER: Final = f'!f() {{ echo "password=${_CREDENTIAL_VARIABLE}"; }}; f'
_UNATTENDED: Final = {
    # Nothing here can answer a prompt, so a remote that asks for one has to
    # fail instead of waiting for a terminal that will never come.
    "GIT_TERMINAL_PROMPT": "0",
}
_COMMIT_TIME: Final = "--format=%cI"
_GIT_MISSING: Final = "git is required to mirror a source"


@dataclass(frozen=True, slots=True, kw_only=True)
class GitMirror:
    """A bare clone of one source, refreshed by pulling and read at a commit."""

    source: GitSource
    directory: Path
    credentials: CredentialResolver

    def connect(self) -> Connection:
        """Pull the source's ref, and say what stopped that when it failed."""
        if self.source.credential is None:
            return self._pull(secret=None)
        secret = self.credentials.resolve(self.source.credential)
        if secret is None:
            return Connection(
                state=ConnectionState.CREDENTIAL_UNRESOLVABLE,
                revision=None,
            )
        return self._pull(secret=secret)

    def entries(self, revision: Revision, *, inside: str = "") -> tuple[TreeEntry, ...]:
        """The names one level below `inside` in the tree at this commit."""
        within = (f"{inside}/",) if inside else ()
        listing = self._text("ls-tree", revision.commit, *within)
        return tuple(_tree_entry(line) for line in listing.splitlines())

    def read(self, revision: Revision, path: str) -> bytes:
        """The file's bytes at this commit; nothing is ever checked out."""
        return self._run("cat-file", "blob", f"{revision.commit}:{path}")

    def last_changed_at(self, revision: Revision, path: str) -> datetime:
        """When the last commit up to this one touched that path."""
        stamped = self._text("log", "-1", _COMMIT_TIME, revision.commit, "--", path)
        return datetime.fromisoformat(stamped)

    def _pull(self, *, secret: str | None) -> Connection:
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self._run("init", "--bare", "--quiet")
        ref = self.source.ref
        helper = _credential_helper(secret)
        # The URL is the one argument that may carry a user name, so this is the
        # only call that never turns its command line into an error message.
        pulled = subprocess.run(
            [
                _git_executable(),
                *helper,
                "--git-dir",
                str(self.directory),
                "fetch",
                "--no-tags",
                self.source.url,
                f"+refs/heads/{ref}:refs/heads/{ref}",
            ],
            capture_output=True,
            check=False,
            env=_environment({} if secret is None else {_CREDENTIAL_VARIABLE: secret}),
        )
        if pulled.returncode != 0:
            return Connection(state=ConnectionState.UNREACHABLE, revision=None)
        return Connection(
            state=ConnectionState.READY,
            revision=Revision(
                ref=ref,
                commit=self._text("rev-parse", f"refs/heads/{ref}"),
            ),
        )

    def _text(self, *arguments: str) -> str:
        return self._run(*arguments).decode().strip()

    def _run(self, *arguments: str) -> bytes:
        completed = subprocess.run(
            [_git_executable(), "--git-dir", str(self.directory), *arguments],
            capture_output=True,
            check=False,
            env=_environment({}),
        )
        if completed.returncode != 0:
            message = f"git {' '.join(arguments)}: {completed.stderr.decode().strip()}"
            raise MirrorError(message)
        return completed.stdout


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise GitUnavailableError(_GIT_MISSING)
    return git


def _credential_helper(secret: str | None) -> tuple[str, ...]:
    if secret is None:
        return ()
    return ("-c", f"credential.helper={_CREDENTIAL_HELPER}")


def _environment(extra: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **_UNATTENDED, **extra}


def _tree_entry(line: str) -> TreeEntry:
    attributes, _, path = line.partition("\t")
    _mode, kind, _object = attributes.split()
    return TreeEntry(name=PurePosixPath(path).name, is_directory=kind == "tree")
