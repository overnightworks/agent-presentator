"""Keeps a bare mirror of one source by driving the `git` executable.

git's own program already owns the transports, the credential helpers, and the
prompt suppression an unattended server needs, so this package spawns it rather
than binding a second git implementation (ADR 0010).
"""

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final

from gitmirror.model import (
    Change,
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
# GitHub, GitLab, and every other host that hands out a read-only token accept
# any user name beside it, so an operator whose URL names none still reaches a
# host that insists on one. A URL that does name one keeps it: verified with a
# real git that a helper's `username=` answer always wins over the request's
# own, so the helper reads the request on stdin and only fills the gap.
CREDENTIAL_USER_NAME: Final = "token"
# git asks a helper for the credentials of an https remote, and answering out
# of the child's environment keeps the secret off the command line and off
# disk. `grep` reads the request git writes to stdin before it reads the
# helper's own stdout, which is where a `username=` line already stands when
# the URL named one.
_CREDENTIAL_HELPER: Final = (
    '!f() { grep -q "^username=" || echo "username='
    f'{CREDENTIAL_USER_NAME}"; echo "password=${_CREDENTIAL_VARIABLE}"; }}; f'
)
_SSH_CONNECT_SECONDS: Final = 10
# Only what git and ssh need to run; the child inherits nothing else, so no
# machine-wide askpass helper can hang a pull and no machine-wide trace setting
# can write the secret into a file.
_INHERITED: Final = ("PATH", "HOME")
_UNATTENDED: Final = {
    # Nothing here can answer a prompt, so a remote that asks for one has to
    # fail instead of waiting for a terminal that will never come.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": (
        f"ssh -o BatchMode=yes -o ConnectTimeout={_SSH_CONNECT_SECONDS}"
    ),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
# git prints exactly what the format asks for, so the commit and the time of
# one change arrive on a single line with this between them.
_LAST_CHANGE: Final = "--format=%H %cI"
_BETWEEN_THEM: Final = " "
_GIT_MISSING: Final = "git is required to mirror a source"
_UNREACHABLE: Final = Connection(state=ConnectionState.UNREACHABLE, revision=None)
_REFUSED: Final = Connection(state=ConnectionState.REFUSED, revision=None)
_CREDENTIAL_UNRESOLVABLE: Final = Connection(
    state=ConnectionState.CREDENTIAL_UNRESOLVABLE,
    revision=None,
)
# git's own words for a login the far end turned down, lowercased so a case
# git happens to pick never hides the match. Anything else a fetch can fail
# with — a host that never answers, a name that resolves to nothing — stays
# unreachable instead.
_AUTHENTICATION_WORDS: Final = (
    "authentication failed",
    "invalid username or token",
    "could not read username",
    "could not read password",
    "error: 401",
    "error: 403",
)
# Only the scheme and the host stand in a log line; a user name or a password
# git's own transport carried in the URL never does, even though the secret
# itself only ever reaches git through the environment and cannot appear here.
_USERINFO: Final = re.compile(r"://[^/@]*@")

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class GitMirror:
    """A bare clone of one source, refreshed by pulling and read at a commit."""

    source: GitSource
    directory: Path
    credentials: CredentialResolver
    pull_timeout: timedelta

    def connect(self) -> Connection:
        """Pull the source's ref, and say what stopped that when it failed."""
        if self.source.credential is None:
            return self._pull(secret=None)
        secret = self.credentials.resolve(self.source.credential)
        if secret is None:
            return _CREDENTIAL_UNRESOLVABLE
        return self._pull(secret=secret)

    def entries(self, revision: Revision, *, inside: str = "") -> tuple[TreeEntry, ...]:
        """The names one level below `inside` in the tree at this commit."""
        within = (f"{inside}/",) if inside else ()
        listing = self._text("ls-tree", revision.commit, *within)
        return tuple(_tree_entry(line) for line in listing.splitlines())

    def read(self, revision: Revision, path: str) -> bytes:
        """The file's bytes at this commit; nothing is ever checked out."""
        return self._run("cat-file", "blob", f"{revision.commit}:{path}")

    def export(self, revision: Revision, path: str, *, into: Path) -> None:
        """Write the tree under that path at this commit into that directory.

        A caller that has to run a tool over the files needs them as files; the
        mirror stays bare, so the tree is written out beside it rather than
        checked out into it. Git carries no empty directory, so a path with
        nothing under it is a path this commit does not carry.
        """
        entries = self.entries(revision, inside=path)
        if not entries:
            message = f"{revision.commit} carries no {path}"
            raise MirrorError(message)
        into.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            below = f"{path}/{entry.name}"
            if entry.is_directory:
                self.export(revision, below, into=into / entry.name)
            else:
                (into / entry.name).write_bytes(self.read(revision, below))

    def last_change(self, revision: Revision, path: str) -> Change:
        """The last commit up to this one that touched that path, and when.

        A caller that asks per path gets a value that stands still while other
        paths move, which is what tells one changed folder from an untouched
        one.
        """
        stamped = self._text("log", "-1", _LAST_CHANGE, revision.commit, "--", path)
        commit, _, at = stamped.partition(_BETWEEN_THEM)
        return Change(commit=commit, at=datetime.fromisoformat(at))

    def _pull(self, *, secret: str | None) -> Connection:
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self._run("init", "--bare", "--quiet")
        ref = self.source.ref
        # The URL is the one argument that may carry a user name, so this is the
        # only call that never turns its command line into an error message.
        try:
            pulled = subprocess.run(
                [
                    _git_executable(),
                    *credential_arguments(secret),
                    "--git-dir",
                    str(self.directory),
                    "fetch",
                    "--no-tags",
                    self.source.url,
                    f"+refs/heads/{ref}:refs/heads/{ref}",
                ],
                capture_output=True,
                check=False,
                env=unattended_environment(secret),
                timeout=self.pull_timeout.total_seconds(),
            )
        except subprocess.TimeoutExpired:
            # A remote nobody can reach must not hold the page that asked for it.
            return _UNREACHABLE
        if pulled.returncode != 0:
            stderr = pulled.stderr.decode(errors="replace").strip()
            _log.warning(
                "git fetch of %s failed: %s",
                _without_userinfo(self.source.url),
                stderr,
            )
            return _REFUSED if _names_authentication(stderr) else _UNREACHABLE
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
            env=unattended_environment(secret=None),
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


def credential_arguments(secret: str | None) -> tuple[str, ...]:
    """The `-c` options git is run with, carrying no secret value itself."""
    # An empty helper clears whatever a config file or the environment set, so
    # only the helper named here can ever answer.
    cleared = ("-c", "credential.helper=")
    if secret is None:
        return cleared
    return (*cleared, "-c", f"credential.helper={_CREDENTIAL_HELPER}")


def unattended_environment(secret: str | None) -> dict[str, str]:
    """The whole environment git runs in, with the secret only when there is one."""
    inherited = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    carried = {} if secret is None else {_CREDENTIAL_VARIABLE: secret}
    return {**inherited, **_UNATTENDED, **carried}


def _names_authentication(stderr: str) -> bool:
    """Whether a failed fetch's own words say the far end turned the login down."""
    lowered = stderr.lower()
    return any(word in lowered for word in _AUTHENTICATION_WORDS)


def _without_userinfo(url: str) -> str:
    """That URL with any embedded user name or password hidden."""
    return _USERINFO.sub("://***@", url)


def _tree_entry(line: str) -> TreeEntry:
    attributes, _, path = line.partition("\t")
    _mode, kind, _object = attributes.split()
    return TreeEntry(name=PurePosixPath(path).name, is_directory=kind == "tree")
