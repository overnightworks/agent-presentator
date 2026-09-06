"""The mirror against a real bare repository in a temporary directory."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gitmirror.mirror import GitMirror
from gitmirror.model import (
    ConnectionState,
    CredentialReference,
    GitSource,
    GitUnavailableError,
    MirrorError,
    Revision,
)
from tests.conftest import MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_TOKEN_REFERENCE = CredentialReference(name="A_READ_ONLY_TOKEN")
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
    """A resolver that answers the reference the source names."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return f"the secret behind {reference.name}"


def a_mirror(
    url: str,
    *,
    directory: Path,
    credential: CredentialReference | None = None,
    resolver: NoSecretAnywhere | OneKnownSecret | None = None,
) -> GitMirror:
    return GitMirror(
        source=GitSource(url=url, ref=MAIN_BRANCH, credential=credential),
        directory=directory / "mirror.git",
        credentials=NoSecretAnywhere() if resolver is None else resolver,
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
    assert mirror.last_changed_at(revision, "hello-deck") == _PUSHED_AT


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
        resolver=NoSecretAnywhere(),
    )

    connection = unresolvable.connect()

    assert connection.state is ConnectionState.CREDENTIAL_UNRESOLVABLE
    assert connection.revision is None


def test_a_source_with_a_resolved_credential_is_pulled(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit(_A_DECK, at=_PUSHED_AT)
    authenticated = a_mirror(
        remote.url,
        directory=tmp_path,
        credential=_TOKEN_REFERENCE,
        resolver=OneKnownSecret(),
    )

    connection = authenticated.connect()

    assert connection.state is ConnectionState.READY


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
