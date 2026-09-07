"""The mirror against a real bare repository in a temporary directory."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitmirror.mirror import (
    GitMirror,
    credential_arguments,
    unattended_environment,
)
from gitmirror.model import (
    Change,
    ConnectionState,
    CredentialReference,
    GitSource,
    GitUnavailableError,
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


class NoSecretAnywhere:
    """A resolver for a reference that leads nowhere."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return None


class OneKnownSecret:
    """A resolver that answers whatever reference the source names."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return f"{_WHAT_THE_RESOLVER_ANSWERS} for {reference.name}"


def a_mirror(
    url: str,
    *,
    directory: Path,
    credential: CredentialReference | None = None,
    resolver: NoSecretAnywhere | OneKnownSecret | None = None,
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


def test_a_source_that_cannot_be_reached_is_named_unreachable(
    tmp_path: Path,
) -> None:
    missing = a_mirror((tmp_path / "nothing.git").as_uri(), directory=tmp_path)

    connection = missing.connect()

    assert connection.state is ConnectionState.UNREACHABLE
    assert connection.revision is None


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


def test_a_resolved_credential_reaches_git_without_riding_on_its_command_line() -> None:
    arguments = credential_arguments(_WHAT_THE_RESOLVER_ANSWERS)

    answered = subprocess.run(
        ["git", *arguments, "credential", "fill"],
        capture_output=True,
        check=True,
        env=unattended_environment(_WHAT_THE_RESOLVER_ANSWERS),
        input="protocol=https\nhost=git.example\nusername=token-user\n\n",
        text=True,
    )

    assert f"password={_WHAT_THE_RESOLVER_ANSWERS}" in answered.stdout
    assert _WHAT_THE_RESOLVER_ANSWERS not in " ".join(arguments)


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


def test_a_resolved_credential_tells_an_unreachable_remote_from_a_missing_secret(
    tmp_path: Path,
) -> None:
    resolved = a_mirror(
        (tmp_path / "nothing.git").as_uri(),
        directory=tmp_path,
        credential=_TOKEN_REFERENCE,
        resolver=OneKnownSecret(),
    )

    connection = resolved.connect()

    assert connection.state is ConnectionState.UNREACHABLE


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
