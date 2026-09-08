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
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final

from gitmirror.model import (
    Change,
    Connection,
    ConnectionCheck,
    ConnectionState,
    CredentialResolver,
    GitSource,
    GitUnavailableError,
    InvalidCredentialError,
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
# the URL named one. `printf '%s\n'` with a constant format, never `echo`,
# because a shell's `echo` reinterprets a backslash sequence inside the value
# it is given — `dash`'s always does, verified with a real git — which can
# truncate or reshape a token that merely happens to contain one.
_CREDENTIAL_HELPER: Final = (
    "!f() { grep -q '^username=' || printf 'username=%s\\n' "
    f"'{CREDENTIAL_USER_NAME}'; "
    f'printf "password=%s\\n" "${_CREDENTIAL_VARIABLE}"; }}; f'
)
# git's line-based credential protocol reads one field per line; a literal CR
# or LF inside a secret would forge a second line no `printf` quoting can
# undo, so the boundary where a resolved secret enters the mirror rejects one
# outright rather than hand git a broken request.
_UNSAFE_CREDENTIAL: Final = (
    "a source's credential carries a control character and cannot be sent to git"
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
# A deploy key's private half lives on disk only for the length of one fetch
# (ADR 0013): a 0700 directory holding one 0600 file, removed in a `finally`
# whatever the fetch did.
_SSH_KEY_DIRECTORY_MODE: Final = 0o700
_SSH_KEY_FILE_MODE: Final = 0o600
_SSH_KEY_FILE_NAME: Final = "id_ed25519"
_KNOWN_HOSTS_FILE_NAME: Final = "known_hosts"
# git prints exactly what the format asks for, so the commit and the time of
# one change arrive on a single line with this between them.
_LAST_CHANGE: Final = "--format=%H %cI"
_BETWEEN_THEM: Final = " "
_GIT_MISSING: Final = "git is required to mirror a source"
_UNREACHABLE: Final = Connection(state=ConnectionState.UNREACHABLE, revision=None)
_CREDENTIAL_UNRESOLVABLE: Final = Connection(
    state=ConnectionState.CREDENTIAL_UNRESOLVABLE,
    revision=None,
)
_REFUSED_CHECK: Final = ConnectionCheck(
    state=ConnectionState.REFUSED,
    commit=None,
    detail=None,
)
_UNREACHABLE_CHECK: Final = ConnectionCheck(
    state=ConnectionState.UNREACHABLE,
    commit=None,
    detail=None,
)
# git's own words for a login the far end named and turned down, lowercased so
# a case git happens to pick never hides the match. A bare 403 is deliberately
# absent: a proxy or a WAF answers that status for reasons that have nothing
# to do with the token, so only a message git itself ties to a login counts.
_REFUSED_WORDS: Final = (
    "authentication failed",
    "invalid username or token",
    "returned error: 401",
    # ssh's own words for a deploy key the far end no longer accepts.
    "permission denied (publickey)",
)
# git's own words for a host that never answered at all — no TCP connection,
# no DNS name, no reply before the timeout above already caught it.
_UNREACHABLE_WORDS: Final = (
    "could not resolve host",
    "failed to connect",
    "connection refused",
    "connection timed out",
)
# Any `scheme://user:pass@` a URL can carry, wherever it stands in the text:
# git reflects a redirect target or the remote's own reply verbatim, so this
# runs over the whole diagnostic, not only the source's own URL.
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
        _reject_unsafe_secret(secret)
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
            with _credentials(self.source.url, secret) as (credential, ssh_key):
                pulled = subprocess.run(
                    [
                        _git_executable(),
                        *credential_arguments(credential),
                        "--git-dir",
                        str(self.directory),
                        "fetch",
                        "--no-tags",
                        "--",
                        self.source.url,
                        f"+refs/heads/{ref}:refs/heads/{ref}",
                    ],
                    capture_output=True,
                    check=False,
                    env=unattended_environment(
                        credential,
                        ssh_key=ssh_key,
                        known_hosts=self.directory.parent / _KNOWN_HOSTS_FILE_NAME,
                    ),
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
                _sanitized(stderr, secret=secret),
            )
            return Connection(state=connection_state_for_failure(stderr), revision=None)
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


def check_connection(
    *,
    url: str,
    ref: str,
    secret: str | None,
    timeout: timedelta,
) -> ConnectionCheck:
    """Ask the remote for one ref's head, without ever writing a mirror to disk.

    `ls-remote` alone tells an admin whether the server can read the
    repository before Add source stores anything, through the same
    credential helper, environment, and classifier `_pull` uses.
    """
    if secret is not None:
        try:
            _reject_unsafe_secret(secret)
        except InvalidCredentialError:
            return _REFUSED_CHECK
    try:
        probed = subprocess.run(
            [
                _git_executable(),
                *credential_arguments(secret),
                "ls-remote",
                "--exit-code",
                "--",
                url,
                f"refs/heads/{ref}",
            ],
            capture_output=True,
            check=False,
            env=unattended_environment(secret),
            timeout=timeout.total_seconds(),
        )
    except subprocess.TimeoutExpired:
        return _UNREACHABLE_CHECK
    if probed.returncode != 0:
        stderr = probed.stderr.decode(errors="replace").strip()
        sanitized = _sanitized(stderr, secret=secret)
        return ConnectionCheck(
            state=connection_state_for_failure(stderr),
            commit=None,
            detail=sanitized.splitlines()[0] if sanitized else None,
        )
    commit, _, _ = probed.stdout.decode().strip().partition("\t")
    return ConnectionCheck(
        state=ConnectionState.READY,
        commit=commit or None,
        detail=None,
    )


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


def unattended_environment(
    secret: str | None,
    *,
    ssh_key: Path | None = None,
    known_hosts: Path | None = None,
) -> dict[str, str]:
    """The whole environment git runs in, with the secret only when there is one.

    An ssh key file overrides `GIT_SSH_COMMAND` to name it, so a deploy-key
    fetch authenticates through that file rather than through any identity
    the machine's own `ssh` would otherwise offer.
    """
    inherited = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    carried = {} if secret is None else {_CREDENTIAL_VARIABLE: secret}
    environment = {**inherited, **_UNATTENDED, **carried}
    if ssh_key is not None and known_hosts is not None:
        environment["GIT_SSH_COMMAND"] = _ssh_command(ssh_key, known_hosts)
    return environment


def is_ssh_remote(url: str) -> bool:
    """Whether this git remote is reached over ssh: `ssh://` or the scp form."""
    return url.startswith("ssh://") or ("://" not in url and "@" in url)


def _ssh_command(key: Path, known_hosts: Path) -> str:
    """The `ssh` git is told to run: this key alone, host key pinned on first use."""
    return (
        f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes "
        f"-o ConnectTimeout={_SSH_CONNECT_SECONDS} -o StrictHostKeyChecking=accept-new "
        f"-o UserKnownHostsFile={known_hosts}"
    )


@contextmanager
def _credentials(
    url: str,
    secret: str | None,
) -> Generator[tuple[str | None, Path | None]]:
    """The secret to hand git's credential helper, and an ssh key file when needed.

    An ssh remote authenticates through the key file alone, never through the
    helper, so the secret travels as a file rather than as the environment
    value the helper would otherwise read.
    """
    if secret is not None and is_ssh_remote(url):
        with _ssh_key_file(secret) as key:
            yield None, key
    else:
        yield secret, None


@contextmanager
def _ssh_key_file(secret: str) -> Generator[Path]:
    """A deploy key's private half, written for the length of one fetch (ADR 0013).

    A 0700 directory holding one 0600 file, both gone in the `finally`
    whatever the fetch did with them.
    """
    directory = Path(tempfile.mkdtemp(prefix="gitmirror-deploy-key-"))
    directory.chmod(_SSH_KEY_DIRECTORY_MODE)
    key = directory / _SSH_KEY_FILE_NAME
    try:
        key.write_text(secret)
        key.chmod(_SSH_KEY_FILE_MODE)
        yield key
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _reject_unsafe_secret(secret: str) -> None:
    """Fail loud rather than hand git's line-based protocol a broken line.

    A secret can reach a mirror from any resolver an instance configures, so
    this is the one place every one of them passes through, whatever a
    resolver's own input validation does or does not already catch.
    """
    if not secret.isprintable():
        raise InvalidCredentialError(_UNSAFE_CREDENTIAL)


def connection_state_for_failure(stderr: str) -> ConnectionState:
    """Which of the three failure states a failed pull's own words say happened.

    Unreachable is only ever one of `_UNREACHABLE_WORDS` or the timeout above;
    an answer git phrases in words this function does not recognise — a TLS
    certificate failure, a proxy's own text — is not proof the host never
    answered, so it stays `failed` rather than guessing.
    """
    lowered = stderr.lower()
    if any(word in lowered for word in _REFUSED_WORDS):
        return ConnectionState.REFUSED
    if any(word in lowered for word in _UNREACHABLE_WORDS):
        return ConnectionState.UNREACHABLE
    return ConnectionState.FAILED


def _without_userinfo(text: str) -> str:
    """That text with any embedded user name or password hidden, wherever it stands."""
    return _USERINFO.sub("://***@", text)


def _sanitized(stderr: str, *, secret: str | None) -> str:
    """That stderr with every userinfo and the secret itself erased.

    The secret only ever reaches git through the child's environment, so it
    cannot appear here on its own account; this still erases it, because a
    defence a probe can falsify is not a defence.
    """
    redacted = _without_userinfo(stderr)
    if secret:
        redacted = redacted.replace(secret, "***")
    return redacted


def _tree_entry(line: str) -> TreeEntry:
    attributes, _, path = line.partition("\t")
    _mode, kind, _object = attributes.split()
    return TreeEntry(name=PurePosixPath(path).name, is_directory=kind == "tree")
