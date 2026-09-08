"""The mirror against a real bare repository in a temporary directory."""

import base64
import functools
import logging
import socket
import subprocess
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import BaseServer
from typing import cast

import pytest

from gitmirror.mirror import (
    CREDENTIAL_USER_NAME,
    GitMirror,
    connection_state_for_failure,
    credential_arguments,
    unattended_environment,
)
from gitmirror.model import (
    Change,
    ConnectionState,
    CredentialReference,
    GitSource,
    GitUnavailableError,
    InvalidCredentialError,
    MirrorError,
    Revision,
)
from tests.conftest import MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_PUSHED_LATER = datetime(2026, 1, 16, 9, tzinfo=UTC)
_A_GENEROUS_BOUND = timedelta(seconds=30)
_NO_BUDGET_AT_ALL = timedelta(0)
_WHAT_THE_RESOLVER_ANSWERS = "what only this test made up"
_TOKEN_REFERENCE = CredentialReference(name="A_READ_ONLY_TOKEN")
_AN_ASSET = "<svg></svg>\n"
_A_DECK = {
    "hello-deck/deck.toml": 'title = "Hello"\n',
    "hello-deck/slides.md": "# Hello\n",
    "README.md": "not a deck\n",
}
_LOOPBACK = "127.0.0.1"
_LEAKED_USERINFO = "leaked-user:leaked-password"
_AN_UNRESOLVABLE_HOST = "http://host.example.invalid/repo.git"


class NoSecretAnywhere:
    """A resolver for a reference that leads nowhere."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return None


class OneKnownSecret:
    """A resolver that answers whatever reference the source names."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return f"{_WHAT_THE_RESOLVER_ANSWERS} for {reference.name}"


@dataclass(slots=True)
class _FixedSecret:
    """A resolver that answers one exact value, whatever the reference names."""

    value: str

    def resolve(self, reference: CredentialReference) -> str | None:
        return self.value


def a_mirror(
    url: str,
    *,
    directory: Path,
    credential: CredentialReference | None = None,
    resolver: NoSecretAnywhere | OneKnownSecret | _FixedSecret | None = None,
    pull_timeout: timedelta = _A_GENEROUS_BOUND,
) -> GitMirror:
    return GitMirror(
        source=GitSource(url=url, ref=MAIN_BRANCH, credential=credential),
        directory=directory / "mirror.git",
        credentials=NoSecretAnywhere() if resolver is None else resolver,
        pull_timeout=pull_timeout,
    )


def a_ready_mirror(remote: GitRemote, tmp_path: Path) -> tuple[GitMirror, Revision]:
    remote.commit(_A_DECK, at=_PUSHED_AT)
    mirror = a_mirror(remote.url, directory=tmp_path)
    connection = mirror.connect()
    assert connection.revision is not None
    return mirror, connection.revision


def _basic_auth(username: str, secret: str) -> str:
    token = base64.b64encode(f"{username}:{secret}".encode()).decode()
    return f"Basic {token}"


def _prepared_bare_repo(remote: GitRemote) -> Path:
    """A pushed commit, with the loose listing git's dumb HTTP client reads."""
    remote.commit(_A_DECK, at=_PUSHED_AT)
    subprocess.run(
        ["git", "update-server-info", "--force"],
        cwd=remote.bare,
        capture_output=True,
        check=True,
    )
    return remote.bare


@dataclass(slots=True)
class _Served:
    """What the fake host remembers about the request it most recently answered."""

    authorization: str | None = None


class _FakeGitHost(HTTPServer):
    """An `HTTPServer` carrying what its handler needs to answer like a real host."""

    def __init__(
        self,
        address: tuple[str, int],
        handler: Callable[..., BaseHTTPRequestHandler],
        *,
        expected_authorization: str | None,
        status: HTTPStatus,
    ) -> None:
        self.expected_authorization = expected_authorization
        self.status = status
        self.served = _Served()
        super().__init__(address, handler)


class DumbHttpHandler(SimpleHTTPRequestHandler):
    """A bare repository over git's dumb HTTP protocol, gated by Basic auth.

    `git update-server-info` writes the loose object and ref listing git's
    dumb HTTP client reads; serving that directory as static files, behind a
    Basic-auth check read off the server that owns this handler, is the whole
    remote a real authenticated fetch needs — no smart-HTTP backend required.
    """

    def do_GET(self) -> None:
        host = cast("_FakeGitHost", self.server)
        host.served.authorization = self.headers.get("Authorization")
        if host.status is not HTTPStatus.OK or self._refused(host):
            self._answer_without_serving(host)
            return
        super().do_GET()

    def _refused(self, host: _FakeGitHost) -> bool:
        return (
            host.expected_authorization is not None
            and host.served.authorization != host.expected_authorization
        )

    def _answer_without_serving(self, host: _FakeGitHost) -> None:
        answer = (
            host.status if host.status is not HTTPStatus.OK else HTTPStatus.UNAUTHORIZED
        )
        self.send_response(answer)
        if answer is HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
        self.end_headers()

    def log_message(self, *_args: object, **_kwargs: object) -> None:
        """Silence; the assertion is what a test reports, not the socket chatter."""


@contextmanager
def _dumb_http_remote(
    directory: Path,
    *,
    expected_authorization: str | None = None,
    status: HTTPStatus = HTTPStatus.OK,
) -> Generator[tuple[str, _Served], None, None]:
    """A real HTTP server answering one bare repository, port picked free."""
    handler = functools.partial(DumbHttpHandler, directory=str(directory))
    server = _FakeGitHost(
        (_LOOPBACK, 0),
        handler,
        expected_authorization=expected_authorization,
        status=status,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{_LOOPBACK}:{server.server_port}/", server.served
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class RespondingHandler(BaseHTTPRequestHandler):
    """Every request meets one canned status and body, whatever git asks for."""

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        status: HTTPStatus,
        body: bytes,
    ) -> None:
        """Remember the canned answer before the base class starts serving it."""
        self._status = status
        self._body = body
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        self.send_response(self._status)
        if self._status is HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(self._body)

    def log_message(self, *_args: object, **_kwargs: object) -> None:
        """Silence; the assertion is what a test reports, not the socket chatter."""


@contextmanager
def _responding_remote(
    *, status: HTTPStatus, body: bytes = b""
) -> Generator[str, None, None]:
    """A local HTTP server that answers one status and body to every request."""
    handler = functools.partial(RespondingHandler, status=status, body=body)
    server = HTTPServer((_LOOPBACK, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{_LOOPBACK}:{server.server_port}/repo.git"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_pulling_a_source_brings_its_tree_and_names_the_commit(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    mirror, revision = a_ready_mirror(remote, tmp_path)

    top = mirror.entries(revision)
    inside = mirror.entries(revision, inside="hello-deck")

    assert revision.ref == MAIN_BRANCH
    assert {(entry.name, entry.is_directory) for entry in top} == {
        ("hello-deck", True),
        ("README.md", False),
    }
    assert {entry.name for entry in inside} == {"deck.toml", "slides.md"}
    assert mirror.read(revision, "hello-deck/deck.toml") == b'title = "Hello"\n'
    assert mirror.last_change(revision, "hello-deck") == Change(
        commit=revision.commit,
        at=_PUSHED_AT,
    )


def test_a_folders_tree_is_written_out_as_the_files_it_holds(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(
        {**_A_DECK, "hello-deck/images/cover.svg": _AN_ASSET},
        at=_PUSHED_AT,
    )
    mirror = a_mirror(remote.url, directory=tmp_path)
    revision = mirror.connect().revision
    assert revision is not None
    written = tmp_path / "work" / "hello-deck"

    mirror.export(revision, "hello-deck", into=written)

    assert sorted(path.name for path in written.rglob("*")) == [
        "cover.svg",
        "deck.toml",
        "images",
        "slides.md",
    ]
    assert (written / "slides.md").read_bytes() == mirror.read(
        revision,
        "hello-deck/slides.md",
    )
    assert (written / "images" / "cover.svg").read_text(encoding="utf-8") == _AN_ASSET
    assert not (written / "README.md").exists()


def test_a_tree_is_written_out_as_it_stood_at_the_commit_it_is_asked_for(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    mirror, first = a_ready_mirror(remote, tmp_path)
    remote.commit({"hello-deck/slides.md": "# Hello again\n"}, at=_PUSHED_AT)
    assert mirror.connect().revision is not None
    written = tmp_path / "work" / "hello-deck"

    mirror.export(first, "hello-deck", into=written)

    assert (written / "slides.md").read_text(encoding="utf-8") == "# Hello\n"


def test_a_folder_the_commit_does_not_carry_is_refused_rather_than_empty(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    mirror, revision = a_ready_mirror(remote, tmp_path)

    with pytest.raises(MirrorError, match="never-pushed"):
        mirror.export(revision, "never-pushed", into=tmp_path / "work")


def test_pulling_again_brings_what_was_pushed_since(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    mirror, first = a_ready_mirror(remote, tmp_path)

    remote.commit({"hello-deck/slides.md": "# Hello again\n"}, at=_PUSHED_AT)
    second = mirror.connect().revision

    assert second is not None
    assert second.commit != first.commit
    assert mirror.read(second, "hello-deck/slides.md") == b"# Hello again\n"


def test_a_path_untouched_by_the_newest_commit_keeps_the_change_that_touched_it(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    mirror, first = a_ready_mirror(remote, tmp_path)

    remote.commit({"another-deck/slides.md": "# Elsewhere\n"}, at=_PUSHED_LATER)
    second = mirror.connect().revision

    assert second is not None
    assert mirror.last_change(second, "hello-deck") == Change(
        commit=first.commit,
        at=_PUSHED_AT,
    )
    assert mirror.last_change(second, "another-deck") == Change(
        commit=second.commit,
        at=_PUSHED_LATER,
    )


def test_a_credential_reference_that_resolves_to_nothing_stops_the_pull(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(_A_DECK, at=_PUSHED_AT)
    unresolvable = a_mirror(
        remote.url,
        directory=tmp_path,
        credential=_TOKEN_REFERENCE,
    )

    connection = unresolvable.connect()

    assert connection.state is ConnectionState.CREDENTIAL_UNRESOLVABLE
    assert connection.revision is None


def _fill(request: str, *, secret: str = _WHAT_THE_RESOLVER_ANSWERS) -> str:
    """What git's own credential protocol answers for that request on stdin.

    Driving `git credential fill` against the helper's actual `-c` options is
    the cheapest honest proof of the protocol: it is real git merging the
    helper's answer into a real credential, the same merge `git fetch` itself
    performs before it ever opens a connection.
    """
    arguments = credential_arguments(secret)
    return subprocess.run(
        ["git", *arguments, "credential", "fill"],
        capture_output=True,
        check=True,
        env=unattended_environment(secret),
        input=request,
        text=True,
    ).stdout


def test_a_resolved_credential_reaches_git_without_riding_on_its_command_line() -> None:
    arguments = credential_arguments(_WHAT_THE_RESOLVER_ANSWERS)

    answered = _fill("protocol=https\nhost=git.example\nusername=token-user\n\n")

    assert f"password={_WHAT_THE_RESOLVER_ANSWERS}" in answered
    assert _WHAT_THE_RESOLVER_ANSWERS not in " ".join(arguments)


def test_the_helper_keeps_a_user_name_the_url_already_carries() -> None:
    # Verified with real git: a helper that answers `username=` unconditionally
    # overrides the request's own, so the helper must only fill a gap.
    answered = _fill("protocol=https\nhost=git.example\nusername=token-user\n\n")

    assert answered.splitlines() == [
        "protocol=https",
        "host=git.example",
        "username=token-user",
        f"password={_WHAT_THE_RESOLVER_ANSWERS}",
    ]


def test_the_helper_answers_a_user_name_when_the_url_carries_none() -> None:
    answered = _fill("protocol=https\nhost=git.example\n\n")

    assert f"username={CREDENTIAL_USER_NAME}" in answered


@pytest.mark.parametrize(
    "secret",
    [
        "sekret\\cXYZ",
        "trailing-backslash\\",
        "looks-like-a-newline-escape\\nbut-is-not",
    ],
)
def test_the_helper_preserves_a_backslash_sequence_in_the_secret(secret: str) -> None:
    # `echo` without `-e` still expands a backslash escape on a shell whose
    # builtin behaves like `echo -e` (verified with real git on this machine's
    # `/bin/sh`); `printf '%s\n'` never reinterprets the value it is given.
    answered = _fill("protocol=https\nhost=git.example\n\n", secret=secret)

    assert f"password={secret}" in answered


@pytest.mark.parametrize(
    "unsafe_secret", ["with\na newline", "with\ra carriage return"]
)
def test_a_secret_carrying_a_control_character_is_rejected_before_it_reaches_git(
    unsafe_secret: str,
    tmp_path: Path,
) -> None:
    # git's credential protocol is one field per line; a literal CR or LF
    # inside the secret would forge a second line no shell quoting can undo,
    # so the mirror refuses it outright rather than hand git a broken request.
    mirror = a_mirror(
        "https://git.example.invalid/repo.git",
        directory=tmp_path,
        credential=_TOKEN_REFERENCE,
        resolver=_FixedSecret(unsafe_secret),
    )

    with pytest.raises(InvalidCredentialError):
        mirror.connect()


def test_git_runs_without_the_machine_settings_that_could_hang_or_leak_it() -> None:
    environment = unattended_environment(None)

    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in environment["GIT_SSH_COMMAND"]
    assert "GITMIRROR_CREDENTIAL" not in environment
    assert set(environment) <= {
        "PATH",
        "HOME",
        "GIT_TERMINAL_PROMPT",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
    }


def test_a_pull_that_runs_past_its_bound_is_named_unreachable(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(_A_DECK, at=_PUSHED_AT)
    impatient = a_mirror(
        remote.url,
        directory=tmp_path,
        pull_timeout=_NO_BUDGET_AT_ALL,
    )

    connection = impatient.connect()

    assert connection.state is ConnectionState.UNREACHABLE
    assert connection.revision is None


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        pytest.param(
            "fatal: Authentication failed for 'https://host.example/repo.git/'",
            ConnectionState.REFUSED,
            id="authentication-failed-footer",
        ),
        pytest.param(
            "remote: Invalid username or token.\n"
            "fatal: unable to access 'https://host.example/repo.git/': "
            "The requested URL returned error: 403",
            ConnectionState.REFUSED,
            id="invalid-username-or-token-body",
        ),
        pytest.param(
            "fatal: unable to access 'https://host.example/repo.git/': "
            "The requested URL returned error: 401",
            ConnectionState.REFUSED,
            id="bare-401-status",
        ),
        pytest.param(
            "fatal: unable to access 'https://host.example/repo.git/': "
            "Could not resolve host: host.example",
            ConnectionState.UNREACHABLE,
            id="could-not-resolve-host",
        ),
        pytest.param(
            "fatal: unable to access 'https://host.example/repo.git/': "
            "SSL certificate problem: unable to get local issuer certificate",
            ConnectionState.FAILED,
            id="a-tls-certificate-failure-is-not-proof-the-host-never-answered",
        ),
        pytest.param(
            "fatal: unable to access 'https://host.example/repo.git/': "
            "Failed to connect to host.example port 443: Couldn't connect to server",
            ConnectionState.UNREACHABLE,
            id="failed-to-connect",
        ),
        pytest.param(
            "fatal: unable to access 'https://host.example/repo.git/': "
            "Connection timed out",
            ConnectionState.UNREACHABLE,
            id="connection-timed-out",
        ),
        pytest.param(
            "remote: Repository not found.\n"
            "fatal: repository 'https://host.example/repo.git/' not found",
            ConnectionState.FAILED,
            id="repository-not-found",
        ),
        pytest.param(
            "remote: Forbidden by WAF\n"
            "fatal: unable to access 'https://host.example/repo.git/': "
            "The requested URL returned error: 403",
            ConnectionState.FAILED,
            id="bare-403-without-auth-wording",
        ),
        pytest.param(
            "fatal: could not read Username for 'https://host.example': "
            "terminal prompts disabled",
            ConnectionState.FAILED,
            id="no-credential-configured-at-all",
        ),
    ],
)
def test_a_failed_pulls_own_words_choose_the_reason(
    stderr: str,
    expected: ConnectionState,
) -> None:
    assert connection_state_for_failure(stderr) is expected


def test_a_url_without_a_user_name_authenticates_as_the_constant_user(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    bare = _prepared_bare_repo(remote)
    resolved = f"{_WHAT_THE_RESOLVER_ANSWERS} for {_TOKEN_REFERENCE.name}"
    expected = _basic_auth(CREDENTIAL_USER_NAME, resolved)
    with _dumb_http_remote(bare, expected_authorization=expected) as (url, served):
        mirror = a_mirror(
            url,
            directory=tmp_path,
            credential=_TOKEN_REFERENCE,
            resolver=OneKnownSecret(),
        )
        connection = mirror.connect()

    assert connection.state is ConnectionState.READY
    assert connection.revision is not None
    assert served.authorization == expected


def test_a_url_with_its_own_user_name_keeps_it(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    bare = _prepared_bare_repo(remote)
    resolved = f"{_WHAT_THE_RESOLVER_ANSWERS} for {_TOKEN_REFERENCE.name}"
    expected = _basic_auth("alice", resolved)
    with _dumb_http_remote(bare, expected_authorization=expected) as (url, served):
        with_own_username = url.replace("://", "://alice@")
        mirror = a_mirror(
            with_own_username,
            directory=tmp_path,
            credential=_TOKEN_REFERENCE,
            resolver=OneKnownSecret(),
        )
        connection = mirror.connect()

    assert connection.state is ConnectionState.READY
    assert served.authorization == expected


def test_a_wrong_secret_is_refused(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    bare = _prepared_bare_repo(remote)
    expected = _basic_auth(CREDENTIAL_USER_NAME, "the-secret-the-host-actually-wants")
    with _dumb_http_remote(bare, expected_authorization=expected) as (url, _served):
        mirror = a_mirror(
            url,
            directory=tmp_path,
            credential=_TOKEN_REFERENCE,
            resolver=OneKnownSecret(),
        )
        connection = mirror.connect()

    assert connection.state is ConnectionState.REFUSED
    assert connection.revision is None


def test_a_host_that_answers_something_other_than_the_repository_is_named_failed(
    tmp_path: Path,
) -> None:
    with _responding_remote(
        status=HTTPStatus.NOT_FOUND,
        body=b"Repository not found.\n",
    ) as url:
        mirror = a_mirror(url, directory=tmp_path)
        connection = mirror.connect()

    assert connection.state is ConnectionState.FAILED
    assert connection.revision is None


def test_an_unresolvable_host_is_named_unreachable(tmp_path: Path) -> None:
    mirror = a_mirror(_AN_UNRESOLVABLE_HOST, directory=tmp_path)

    connection = mirror.connect()

    assert connection.state is ConnectionState.UNREACHABLE
    assert connection.revision is None


def test_a_failed_pull_logs_gits_words_with_the_urls_userinfo_hidden(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gitmirror.mirror")
    with _responding_remote(status=HTTPStatus.UNAUTHORIZED) as remote_url:
        leaking_url = remote_url.replace("//", f"//{_LEAKED_USERINFO}@")
        refused = a_mirror(
            leaking_url,
            directory=tmp_path,
            credential=_TOKEN_REFERENCE,
            resolver=OneKnownSecret(),
        )
        refused.connect()

    leaked_name, _, leaked_password = _LEAKED_USERINFO.partition(":")
    [record] = caplog.records
    assert record.levelname == "WARNING"
    assert "***" in record.message
    assert leaked_name not in record.message
    assert leaked_password not in record.message
    assert _WHAT_THE_RESOLVER_ANSWERS not in record.message
    assert "authentication" in record.message.lower()


def test_a_failed_pull_hides_userinfo_the_remote_reflects_back(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gitmirror.mirror")
    reflected = (
        "retry at https://mirror-user:mirror-secret@mirror.example.invalid/repo.git"
    )
    with _responding_remote(
        status=HTTPStatus.FORBIDDEN,
        body=reflected.encode(),
    ) as remote_url:
        mirror = a_mirror(remote_url, directory=tmp_path)
        mirror.connect()

    [record] = caplog.records
    assert "mirror-user:mirror-secret" not in record.message
    assert "***" in record.message


def test_a_failed_pull_hides_the_secret_the_remote_reflects_back(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gitmirror.mirror")
    resolved = f"{_WHAT_THE_RESOLVER_ANSWERS} for {_TOKEN_REFERENCE.name}"
    with _responding_remote(
        status=HTTPStatus.FORBIDDEN,
        body=f"token rejected: {resolved}".encode(),
    ) as remote_url:
        mirror = a_mirror(
            remote_url,
            directory=tmp_path,
            credential=_TOKEN_REFERENCE,
            resolver=OneKnownSecret(),
        )
        mirror.connect()

    [record] = caplog.records
    assert resolved not in record.message
    assert "***" in record.message


def test_reading_a_path_the_commit_does_not_carry_is_refused(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    mirror, revision = a_ready_mirror(remote, tmp_path)

    with pytest.raises(MirrorError, match="cat-file"):
        mirror.read(revision, "hello-deck/nowhere.md")


def test_a_machine_without_git_refuses_to_mirror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(GitUnavailableError):
        a_mirror("file:///nowhere.git", directory=tmp_path).connect()
