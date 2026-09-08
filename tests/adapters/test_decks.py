"""Deck folders read out of a real repository, and deck rows in a real file."""

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Final

import pytest

from gitmirror.mirror import credential_arguments, unattended_environment
from gitmirror.model import CredentialReference, CredentialResolver
from presentator.adapters.decks import (
    FilesystemLocalMount,
    MirroredConnectionChecker,
    MirroredDeckFolders,
    SourceCredentials,
    SourceMirrors,
    SqliteDeckStore,
    SqliteSourceKeyDrafts,
    SqliteSourceRunStore,
    SqliteSourceStore,
    create_deck_tables,
)
from presentator.adapters.identity import (
    SqliteUserStore,
    TokenIdentifierFactory,
    create_identity_tables,
)
from presentator.adapters.secrets import SecretBox, secret_box
from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.decks import (
    MANIFEST_FILE,
    RECENT_SOURCE_RUNS,
    SLIDES_FILE,
    Build,
    BuildAttempt,
    BuildOutcome,
    Deck,
    DeckFolder,
    DeployKeyDraft,
    SecretLocation,
    Source,
    SourceRun,
    SourceRunFailure,
    SourceRunOutcome,
    SourceWrite,
)
from presentator.contracts.models import Account, Role, User
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_NOTICED_GONE_AT = datetime(2026, 1, 20, 9, tzinfo=UTC)
_OWNER = User(id="the-admin", username="felix", role=Role.ADMIN)
_SOURCE_ID = "the-source-that-carried-it"
_ANOTHER_SOURCE_ID = "a-second-source"
_SOURCE_NAME = "decks"
_ANOTHER_SOURCE_NAME = "talks"
_ADDRESS = "git@example.invalid:decks.git"
_ANOTHER_ADDRESS = "git@example.invalid:talks.git"
_CREDENTIAL_VARIABLE = "A_READ_ONLY_TOKEN"
_INSTANCE_KEY = "an instance key of thirty-two ch"
_ANOTHER_INSTANCE_KEY = "the key another instance carries"
_WHAT_THE_GIT_HOST_EXPECTS = "the read-only words only this test made up"
_A_GENEROUS_BOUND = timedelta(seconds=30)
_NO_BUDGET_AT_ALL = timedelta(0)
_A_STORED_HASH = "the hash first start stored"
_BUILT_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
_TRIED_AT = datetime(2026, 1, 15, 10, tzinfo=UTC)
_WHAT_THE_TOOLCHAIN_SAID = "slides.md:41:3 Unexpected token in frontmatter"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_A_LATER_COMMIT = "b7c1d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"


def a_source(url: str) -> Source:
    """A source whose secret, if it had one, no test of this file resolves."""
    return Source(
        id=_SOURCE_ID,
        name=_SOURCE_NAME,
        url=url,
        ref=MAIN_BRANCH,
        secret_location=None,
        owner_id=_OWNER.id,
    )


def a_box(*, instance_key: str = _INSTANCE_KEY) -> SecretBox:
    return secret_box(instance_key)


@dataclass(frozen=True, slots=True)
class OpenCredentials:
    """A stand-in so a local bare remote can be pulled without a stored secret."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return "unused-on-a-file-url"


def a_resolver(
    database: Path,
    *,
    instance_key: str = _INSTANCE_KEY,
) -> SourceCredentials:
    """The resolver the composition root builds, over that real file."""
    return SourceCredentials(
        database=database,
        box=a_box(instance_key=instance_key),
    )


def folders_under(
    tmp_path: Path,
    *,
    pull_timeout: timedelta = _A_GENEROUS_BOUND,
    credentials: CredentialResolver | None = None,
    local_mount: FilesystemLocalMount | None = None,
) -> MirroredDeckFolders:
    return MirroredDeckFolders(
        mirrors=mirrors_under(
            tmp_path,
            timeout=pull_timeout,
            credentials=credentials,
            local_mount=local_mount,
        ),
    )


def mirrors_under(
    tmp_path: Path,
    *,
    timeout: timedelta = _A_GENEROUS_BOUND,
    credentials: CredentialResolver | None = None,
    local_mount: FilesystemLocalMount | None = None,
) -> SourceMirrors:
    return SourceMirrors(
        directory=tmp_path / "mirrors",
        credentials=OpenCredentials() if credentials is None else credentials,
        pull_timeout=timeout,
        local_mount=(
            FilesystemLocalMount(mount=tmp_path) if local_mount is None else local_mount
        ),
    )


def folders_read(tmp_path: Path, source: Source) -> tuple[DeckFolder, ...]:
    """What a source a test has made readable carries."""
    poll = folders_under(tmp_path).folders(source)
    assert poll.folders is not None
    return poll.folders


def a_deck(
    slug: str = "kundenfeedback",
    *,
    title: str = "Kundenfeedback",
    commit: str = _COMMIT,
    source_id: str = _SOURCE_ID,
) -> Deck:
    return Deck(
        slug=slug,
        title=title,
        changed_at=_PUSHED_AT,
        owner_id=_OWNER.id,
        source_id=source_id,
        commit=commit,
        build=None,
        attempt=None,
    )


def a_build(tmp_path: Path, *, commit: str = _COMMIT) -> Build:
    return Build(
        directory=tmp_path / "builds" / "kundenfeedback" / commit / "talk",
        pdf=tmp_path / "builds" / "kundenfeedback" / commit / "deck.pdf",
        commit=commit,
        built_at=_BUILT_AT,
    )


def an_attempt(
    *,
    outcome: BuildOutcome = BuildOutcome.FAILED,
    failure: str | None = _WHAT_THE_TOOLCHAIN_SAID,
    commit: str = _A_LATER_COMMIT,
) -> BuildAttempt:
    return BuildAttempt(
        commit=commit,
        started_at=_TRIED_AT,
        outcome=outcome,
        failure=failure,
    )


def a_deck_store(tmp_path: Path) -> SqliteDeckStore:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    return SqliteDeckStore(database=database)


def a_source_run_store(tmp_path: Path) -> SqliteSourceRunStore:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    return SqliteSourceRunStore(database=database)


def a_successful_run(*, source_id: str = _SOURCE_ID, at: datetime) -> SourceRun:
    return SourceRun(
        source_id=source_id,
        at=at,
        outcome=SourceRunOutcome.SUCCESS,
        commit=_COMMIT,
        reason=None,
    )


def a_failed_run(*, source_id: str = _SOURCE_ID, at: datetime) -> SourceRun:
    return SourceRun(
        source_id=source_id,
        at=at,
        outcome=SourceRunOutcome.FAILURE,
        commit=None,
        reason=SourceRunFailure.UNREACHABLE,
    )


def a_source_store(
    database: Path,
    *,
    instance_key: str = _INSTANCE_KEY,
) -> SqliteSourceStore:
    """The sources table over that file."""
    return SqliteSourceStore(
        database=database,
        identifiers=TokenIdentifierFactory(),
        box=a_box(instance_key=instance_key),
    )


def add_a_source(
    database: Path,
    write: SourceWrite | None = None,
    *,
    instance_key: str = _INSTANCE_KEY,
) -> Source:
    """A stored source row this instance can open."""
    added = a_source_store(database, instance_key=instance_key).add(
        a_write(name=_SOURCE_NAME, url=_ADDRESS) if write is None else write,
    )
    assert added is not None
    return added


def a_key_drafts_store(
    database: Path,
    *,
    instance_key: str = _INSTANCE_KEY,
) -> SqliteSourceKeyDrafts:
    """The `source_key_drafts` table over that file."""
    return SqliteSourceKeyDrafts(
        database=database,
        identifiers=TokenIdentifierFactory(),
        box=a_box(instance_key=instance_key),
    )


_A_LEFTOVER_ENVIRONMENT_SOURCE: Final = """
INSERT INTO sources (id, name, url, ref, credential_reference, owner_id)
VALUES (?, ?, ?, ?, ?, ?)
"""


def a_leftover_environment_source(
    database: Path,
    *,
    source_id: str = _SOURCE_ID,
    name: str = _SOURCE_NAME,
    url: str = _ADDRESS,
) -> Source:
    """A row written when a source still named an environment variable."""
    with rows(database) as cursor:
        cursor.execute(
            _A_LEFTOVER_ENVIRONMENT_SOURCE,
            (
                source_id,
                name,
                url,
                MAIN_BRANCH,
                _CREDENTIAL_VARIABLE,
                _OWNER.id,
            ),
        )
    return a_source_store(database).all()[0]


# The deck table as this product wrote it before a source was a row of its own,
# so what an upgrade finds is a file and not a description of one.
_DECKS_BEFORE_SOURCES = """
CREATE TABLE decks (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id),
    commit_sha TEXT NOT NULL,
    active_build TEXT,
    pdf_export TEXT,
    built_commit TEXT,
    built_at TEXT,
    removed_at TEXT
);
"""
_A_DECK_BEFORE_SOURCES = """
INSERT INTO decks (slug, title, changed_at, owner_id, commit_sha)
VALUES (?, ?, ?, ?, ?)
"""
# The sources table as this product wrote it while a source's only credential
# was the name of an environment variable.
_SOURCES_BEFORE_ENCRYPTION = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE,
    ref TEXT NOT NULL,
    credential_reference TEXT,
    owner_id TEXT NOT NULL REFERENCES users(id)
);
"""
_A_SOURCE_BEFORE_ENCRYPTION = """
INSERT INTO sources (id, name, url, ref, credential_reference, owner_id)
VALUES (?, ?, ?, ?, ?, ?)
"""
_THE_RAW_COLUMN = "SELECT encrypted_secret FROM sources WHERE id = ?"


def a_database_written_before_sources(tmp_path: Path) -> Path:
    """A file this product wrote before sources were rows, carrying one deck."""
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    SqliteUserStore(database).add_first_account(
        Account(
            id=_OWNER.id,
            username=_OWNER.username,
            role=_OWNER.role,
            password_hash=_A_STORED_HASH,
        ),
    )
    apply_schema(database, _DECKS_BEFORE_SOURCES)
    kept = a_deck()
    with rows(database) as cursor:
        cursor.execute(
            _A_DECK_BEFORE_SOURCES,
            (
                kept.slug,
                kept.title,
                kept.changed_at.isoformat(),
                kept.owner_id,
                kept.commit,
            ),
        )
    return database


def an_instance_that_was_set_up(tmp_path: Path) -> Path:
    """A database with every table and the admin first start created."""
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    create_deck_tables(database)
    SqliteUserStore(database).add_first_account(
        Account(
            id=_OWNER.id,
            username=_OWNER.username,
            role=_OWNER.role,
            password_hash=_A_STORED_HASH,
        ),
    )
    return database


def test_a_pushed_deck_folder_is_read_with_its_title_and_its_change_time(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    folders = folders_read(tmp_path, a_source(remote.url))

    assert len(folders) == 1
    read = folders[0]
    assert read.name == EXAMPLE_SLUG
    assert read.title == EXAMPLE_TITLE
    assert {MANIFEST_FILE, SLIDES_FILE} <= read.file_names
    assert read.changed_at == _PUSHED_AT
    assert read.commit == remote.head


def test_a_push_that_touched_another_folder_leaves_this_decks_commit_alone(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)
    pushed_first = remote.head

    remote.commit({"another-deck/slides.md": "# Elsewhere\n"}, at=_PUSHED_AT)
    read = {
        folder.name: folder for folder in folders_read(tmp_path, a_source(remote.url))
    }

    assert read[EXAMPLE_SLUG].commit == pushed_first
    assert read["another-deck"].commit == remote.head


def test_a_folder_without_a_manifest_is_read_without_a_title(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit({"just-notes/slides.md": "# notes\n"}, at=_PUSHED_AT)

    folders = folders_read(tmp_path, a_source(remote.url))

    assert [folder.title for folder in folders] == [None]


@pytest.mark.parametrize(
    ("broken", "manifest"),
    [
        ("nameless", 'language = "en"\n'),
        ("not-toml", "this is not a manifest at all\n"),
    ],
    ids=["names no title", "is not TOML"],
)
def test_a_folder_whose_manifest_is_unreadable_is_still_there_without_a_title(
    broken: str,
    manifest: str,
    caplog: pytest.LogCaptureFixture,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)
    remote.commit(
        {f"{broken}/deck.toml": manifest, f"{broken}/slides.md": "# x\n"},
        at=_PUSHED_AT,
    )

    with caplog.at_level(logging.WARNING):
        folders = folders_read(tmp_path, a_source(remote.url))

    assert {folder.name: folder.title for folder in folders} == {
        EXAMPLE_SLUG: EXAMPLE_TITLE,
        broken: None,
    }
    assert broken in caplog.text


def test_a_poll_that_reaches_a_source_carries_its_newest_commit(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    poll = folders_under(tmp_path).folders(a_source(remote.url))

    assert poll.commit == remote.head
    assert poll.failure is None


def test_a_pull_that_runs_past_its_bound_says_nothing_rather_than_waiting(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    poll = folders_under(tmp_path, pull_timeout=_NO_BUDGET_AT_ALL).folders(
        a_source(remote.url),
    )

    assert poll.folders is None
    assert poll.commit is None
    assert poll.failure is SourceRunFailure.UNREACHABLE


def test_a_source_that_cannot_be_read_says_nothing_about_its_folders(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    # A host that never answers, not a local path that merely does not exist:
    # the latter now reads its own reason, `failed`, and this test's own is
    # the one a dead host earns.
    unreachable = a_source("http://host.example.invalid/repo.git")

    with caplog.at_level(logging.WARNING):
        poll = folders_under(tmp_path).folders(unreachable)

    assert poll.folders is None
    assert poll.failure is SourceRunFailure.UNREACHABLE
    assert "unreachable" in caplog.text


def a_checker(
    *,
    local_mount: FilesystemLocalMount | None = None,
) -> MirroredConnectionChecker:
    """A checker for a test that never checks a file-kind address."""
    unused_mount = FilesystemLocalMount(mount=Path("/nowhere-a-test-names"))
    return MirroredConnectionChecker(
        check_timeout=_A_GENEROUS_BOUND,
        local_mount=unused_mount if local_mount is None else local_mount,
        known_hosts=Path("/nowhere-a-test-names-known-hosts"),
    )


def test_an_unreachable_check_names_its_own_failure_without_a_commit() -> None:
    checked = a_checker().check(
        url="https://host.example.invalid/repo.git",
        ref=MAIN_BRANCH,
        secret="",
    )

    assert checked.failure is SourceRunFailure.UNREACHABLE
    assert checked.commit is None


def test_a_check_of_an_access_kind_it_does_not_probe_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown address is refused outright, never handed to git at all.

    Nothing this checker probes should ever run `git` against one; a PATH
    with no git on it proves that: a
    call that reached `subprocess.run` would raise `GitUnavailableError`
    instead of answering refused.
    """
    monkeypatch.setenv("PATH", "")

    checked = a_checker().check(
        url="git://host.example.invalid/repo.git",
        ref=MAIN_BRANCH,
        secret="",
    )

    assert checked.failure is SourceRunFailure.REFUSED
    assert checked.commit is None


def test_a_reachable_file_check_answers_the_full_commit(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)

    checked = a_checker(local_mount=FilesystemLocalMount(mount=tmp_path)).check(
        url=remote.url,
        ref=MAIN_BRANCH,
        secret="",
    )

    assert checked.failure is None
    assert checked.commit == remote.head


def test_a_file_check_outside_the_mount_is_refused_without_ever_running_git(
    tmp_path: Path,
) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    outside = tmp_path / "database"
    outside.mkdir()

    checked = a_checker(local_mount=FilesystemLocalMount(mount=mount)).check(
        url=f"file://{outside}",
        ref=MAIN_BRANCH,
        secret="",
    )

    assert checked.failure is SourceRunFailure.REFUSED
    assert checked.commit is None


@pytest.mark.parametrize(
    ("address_template", "accepted"),
    [
        pytest.param("file://{repo}", True, id="file scheme"),
        pytest.param("{repo}", True, id="bare path"),
        pytest.param("{repo}/", True, id="trailing slash"),
        pytest.param(
            "file://{mount}/%2e%2e/database",
            False,
            id="encoded dots climb out of the mount",
        ),
        pytest.param(
            "{mount}/../database",
            False,
            id="literal dots climb out of the mount",
        ),
        pytest.param("file://evil-host{repo}", False, id="a foreign authority"),
        pytest.param("{escape}", False, id="a symlink resolves out of the mount"),
        pytest.param("{outside}", False, id="a sibling directory of the mount"),
    ],
)
def test_a_file_kind_address_is_canonicalized_the_way_git_resolves_it(
    tmp_path: Path,
    address_template: str,
    *,
    accepted: bool,
) -> None:
    """The one place that asks the real filesystem, the way git resolves it.

    A symlink, an encoded or literal `..`, a foreign authority, and a
    sibling of the mount are all refused, while the shapes an ordinary
    address takes all resolve to the one real repository.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    repo = mount / "repo.git"
    repo.mkdir()
    outside = tmp_path / "database"
    outside.mkdir()
    escape = mount / "escape.git"
    escape.symlink_to(outside)
    local_mount = FilesystemLocalMount(mount=mount)
    address = address_template.format(
        repo=repo, mount=mount, escape=escape, outside=outside
    )

    resolved = local_mount.canonical_repository(address)

    assert resolved == (repo.resolve() if accepted else None)


def test_a_credential_free_local_source_fetches_through_the_real_store(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    """A source on this box stores an empty secret, never a missing row.

    The real store, the real encryption box, and the real mirror all agree
    an empty, resolved secret fetches exactly like every other source's
    does, asking git for no credential at all.
    """
    remote.commit_example_deck(at=_PUSHED_AT)
    database = an_instance_that_was_set_up(tmp_path)
    stored = add_a_source(
        database,
        write=SourceWrite(
            name=_SOURCE_NAME,
            url=remote.url,
            ref=MAIN_BRANCH,
            owner_id=_OWNER.id,
            access_secret="",
            hook_secret_hash=b"\x22" * 32,
        ),
    )

    poll = folders_under(
        tmp_path,
        credentials=a_resolver(database),
        local_mount=FilesystemLocalMount(mount=tmp_path),
    ).folders(stored)

    assert poll.failure is None
    assert poll.commit == remote.head


def test_a_file_source_whose_repository_left_the_mount_reads_failed(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A repository that left the mount since it was added is unreadable.

    Moved, deleted, or swapped for a symlink out of the mount, it reads the
    same reason a vanished repository does — never unreachable, which is a
    dead host's word, not a directory's.
    """
    mount = tmp_path / "mount"
    mount.mkdir()
    vanished = a_source(str(mount / "gone.git"))

    with caplog.at_level(logging.WARNING):
        poll = folders_under(
            tmp_path, local_mount=FilesystemLocalMount(mount=mount)
        ).folders(
            vanished,
        )

    assert poll.folders is None
    assert poll.failure is SourceRunFailure.FAILED
    assert "no longer resolves" in caplog.text


def test_removing_a_sources_mirror_deletes_it_from_disk(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)
    mirrors = mirrors_under(tmp_path)
    source = a_source(remote.url)
    mirror = mirrors.of(source)
    assert mirror is not None
    mirror.connect()
    assert mirror.directory.exists()

    gone = mirrors.remove(source)

    assert gone is True
    assert not mirror.directory.exists()


def test_removing_a_mirror_that_was_never_fetched_deletes_nothing_calmly(
    tmp_path: Path,
) -> None:
    gone = mirrors_under(tmp_path).remove(
        a_source("git@example.invalid:never-fetched.git"),
    )

    assert gone is True


def test_a_mirror_whose_parent_refuses_the_delete_is_reported_not_gone(
    remote: GitRemote,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one signal a caller that must not drop a row while this stands needs.

    A directory removed cannot be told from one already gone by the disk
    alone, so the parent is closed rather than the mirror itself: the mirror
    empties out normally and only the last step, taking its own name out of
    the parent, is refused.
    """
    remote.commit_example_deck(at=_PUSHED_AT)
    mirrors = mirrors_under(tmp_path)
    source = a_source(remote.url)
    mirror = mirrors.of(source)
    assert mirror is not None
    mirror.connect()
    parent = mirror.directory.parent
    # Read and traverse stay open, so `exists()` can still tell; only the
    # write bit a delete needs is closed.
    parent.chmod(0o500)
    try:
        with caplog.at_level(logging.WARNING):
            gone = mirrors.remove(source)
    finally:
        parent.chmod(0o700)

    assert gone is False
    assert mirror.directory.exists()
    assert source.name in caplog.text


def test_a_real_git_source_can_be_added_removed_and_added_again(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT)
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)
    folders = MirroredDeckFolders(mirrors=mirrors_under(tmp_path))

    first = sources.add(a_write(name=_SOURCE_NAME, url=remote.url))
    assert first is not None
    first_poll = folders.folders(first)
    assert first_poll.folders is not None

    mirror_gone = folders.forget(first)
    sources.remove(first.id)
    second = sources.add(a_write(name=_SOURCE_NAME, url=remote.url))

    assert mirror_gone is True
    assert second is not None
    assert second.id != first.id
    second_poll = folders.folders(second)
    assert second_poll.folders is not None
    assert second_poll.commit == first_poll.commit


def test_a_stored_secret_stands_in_its_row_as_ciphertext_and_comes_back(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    stored = add_a_source(database)

    with rows(database) as cursor:
        written = cursor.execute(_THE_RAW_COLUMN, (stored.id,)).fetchone()[0]
    assert _WHAT_THE_GIT_HOST_EXPECTS.encode() not in written
    assert stored.secret_location is SecretLocation.STORED
    assert (
        a_resolver(database).resolve(CredentialReference(name=stored.id))
        == _WHAT_THE_GIT_HOST_EXPECTS
    )


def test_a_secret_another_instance_key_wrote_is_refused_rather_than_answered(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    stored = add_a_source(database)

    resolved = a_resolver(database, instance_key=_ANOTHER_INSTANCE_KEY).resolve(
        CredentialReference(name=stored.id),
    )

    assert resolved is None


def test_a_source_carrying_no_secret_and_one_nobody_stored_answer_with_nothing(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    leftover = a_leftover_environment_source(database)
    resolver = a_resolver(database)

    assert leftover.secret_location is None
    assert resolver.resolve(CredentialReference(name=leftover.id)) is None
    assert resolver.resolve(CredentialReference(name="no source of this name")) is None


def test_a_stored_secret_reaches_git_without_riding_on_its_command_line(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    stored = add_a_source(database)

    resolved = a_resolver(database).resolve(CredentialReference(name=stored.id))
    assert resolved == _WHAT_THE_GIT_HOST_EXPECTS
    arguments = credential_arguments(resolved)
    answered = subprocess.run(
        ["git", *arguments, "credential", "fill"],
        capture_output=True,
        check=True,
        env=unattended_environment(resolved),
        input="protocol=https\nhost=git.example\nusername=token-user\n\n",
        text=True,
    )

    assert f"password={_WHAT_THE_GIT_HOST_EXPECTS}" in answered.stdout
    assert not any(_WHAT_THE_GIT_HOST_EXPECTS in argument for argument in arguments)


def test_a_sources_table_written_before_this_column_gains_it_and_keeps_its_row(
    tmp_path: Path,
) -> None:
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    SqliteUserStore(database).add_first_account(
        Account(
            id=_OWNER.id,
            username=_OWNER.username,
            role=_OWNER.role,
            password_hash=_A_STORED_HASH,
        ),
    )
    apply_schema(database, _SOURCES_BEFORE_ENCRYPTION)
    with rows(database) as cursor:
        cursor.execute(
            _A_SOURCE_BEFORE_ENCRYPTION,
            (_SOURCE_ID, _SOURCE_NAME, _ADDRESS, MAIN_BRANCH, None, _OWNER.id),
        )

    create_deck_tables(database)
    sources = a_source_store(database)
    seeded = sources.all()[0]
    sources.put_credential(seeded.id, _WHAT_THE_GIT_HOST_EXPECTS)

    assert seeded.url == _ADDRESS
    assert (
        a_resolver(database).resolve(CredentialReference(name=seeded.id))
        == _WHAT_THE_GIT_HOST_EXPECTS
    )
    assert sources.hook_secret_hash(seeded.name) is None


_A_WEBHOOK_HASH = b"\x11" * 32
_HTTPS_URL = "https://git.example.invalid/talks.git"
_THE_CREDENTIAL_COLUMN = "SELECT credential_reference FROM sources WHERE id = ?"
_THE_ENCRYPTED_OF = "SELECT encrypted_secret FROM sources WHERE id = ?"


def a_write(
    *,
    name: str = "talks",
    url: str = _HTTPS_URL,
    public_key: str | None = None,
) -> SourceWrite:
    return SourceWrite(
        name=name,
        url=url,
        ref=MAIN_BRANCH,
        owner_id=_OWNER.id,
        access_secret=_WHAT_THE_GIT_HOST_EXPECTS,
        hook_secret_hash=_A_WEBHOOK_HASH,
        public_key=public_key,
    )


def test_adding_a_source_stores_the_secret_encrypted_and_the_webhook_hash(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)

    added = sources.add(a_write())

    assert added is not None
    assert added.secret_location is SecretLocation.STORED
    assert sources.hook_secret_hash("talks") == _A_WEBHOOK_HASH
    with rows(database) as cursor:
        written = cursor.execute(_THE_ENCRYPTED_OF, (added.id,)).fetchone()[0]
    assert _WHAT_THE_GIT_HOST_EXPECTS.encode() not in written
    assert (
        a_resolver(database).resolve(CredentialReference(name=added.id))
        == _WHAT_THE_GIT_HOST_EXPECTS
    )


_A_PUBLIC_KEY = "ssh-ed25519 AAAAtestkeymaterial presentator"


def test_adding_an_ssh_source_stores_its_public_key_in_clear(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)

    added = sources.add(a_write(public_key=_A_PUBLIC_KEY))

    assert added is not None
    assert added.public_key == _A_PUBLIC_KEY
    assert sources.all()[0].public_key == _A_PUBLIC_KEY


def test_a_minted_draft_is_this_owners_own_and_reused_while_unconsumed(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)

    minted = drafts.mint(_OWNER.id, at=_PUSHED_AT)

    assert minted.owner_id == _OWNER.id
    assert minted.private_key
    assert minted.public_key
    reused = drafts.unconsumed_for(_OWNER.id, newer_than=_PUSHED_AT)
    assert reused == minted


def test_a_draft_older_than_the_asked_moment_is_not_unconsumed(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    drafts.mint(_OWNER.id, at=_PUSHED_AT)

    assert drafts.unconsumed_for(_OWNER.id, newer_than=_NOTICED_GONE_AT) is None


def test_opening_add_concurrently_reuses_one_draft_for_its_owner(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    concurrent_opens = 2
    start_together = Barrier(concurrent_opens + 1)

    def open_add() -> DeployKeyDraft:
        drafts = a_key_drafts_store(database)
        start_together.wait()
        return drafts.get_or_mint(
            _OWNER.id,
            newer_than=_PUSHED_AT - timedelta(days=1),
            at=_PUSHED_AT,
        )

    with ThreadPoolExecutor(max_workers=concurrent_opens) as pool:
        openings = [pool.submit(open_add) for _ in range(concurrent_opens)]
        start_together.wait()
    opened = [opening.result() for opening in openings]

    assert len(opened) == concurrent_opens
    assert {draft.id for draft in opened} == {opened[0].id}
    with rows(database) as cursor:
        count = cursor.execute("SELECT COUNT(*) FROM source_key_drafts").fetchone()[0]
    assert count == 1


def test_binding_a_draft_by_its_owner_consumes_it(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    minted = drafts.mint(_OWNER.id, at=_PUSHED_AT)

    bound = drafts.bind(minted.id, owner_id=_OWNER.id)

    assert bound == minted
    assert drafts.bind(minted.id, owner_id=_OWNER.id) is None
    assert drafts.unconsumed_for(_OWNER.id, newer_than=_PUSHED_AT) is None


def test_binding_a_draft_another_owner_minted_is_refused(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    minted = drafts.mint("another-admin-entirely", at=_PUSHED_AT)

    assert drafts.bind(minted.id, owner_id=_OWNER.id) is None
    # Refused, not consumed: its own owner can still bind it.
    assert drafts.bind(minted.id, owner_id="another-admin-entirely") == minted


def test_promoting_a_draft_encrypts_its_private_half_and_consumes_it(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    minted = drafts.mint(_OWNER.id, at=_PUSHED_AT)

    promoted = a_source_store(database).add_from_draft(
        a_write(public_key=None),
        draft_id=minted.id,
    )

    assert promoted is not None
    assert promoted.public_key == minted.public_key
    assert drafts.bind(minted.id, owner_id=_OWNER.id) is None
    assert a_resolver(database).resolve(CredentialReference(name=promoted.id)) == (
        minted.private_key
    )
    with rows(database) as cursor:
        encrypted = cursor.execute(_THE_ENCRYPTED_OF, (promoted.id,)).fetchone()[0]
    assert minted.private_key.encode() not in encrypted


@pytest.mark.parametrize(
    "draft_owner",
    [None, "another-admin-entirely"],
    ids=["missing draft", "another account's draft"],
)
def test_promoting_a_missing_or_foreign_draft_creates_no_source(
    tmp_path: Path,
    draft_owner: str | None,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)
    drafts = a_key_drafts_store(database)
    minted = None if draft_owner is None else drafts.mint(draft_owner, at=_PUSHED_AT)

    promoted = sources.add_from_draft(
        a_write(),
        draft_id="a-draft-that-does-not-exist" if minted is None else minted.id,
    )

    assert promoted is None
    assert sources.all() == ()
    if minted is not None:
        assert drafts.bind(minted.id, owner_id=draft_owner) == minted


def test_promoting_an_undecryptable_draft_keeps_it_and_creates_no_source(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)
    minted = a_key_drafts_store(database).mint(_OWNER.id, at=_PUSHED_AT)
    corrupted = b"not fernet ciphertext"
    with rows(database) as cursor:
        cursor.execute(
            "UPDATE source_key_drafts SET encrypted_private_key = ? WHERE id = ?",
            (corrupted, minted.id),
        )

    promoted = sources.add_from_draft(a_write(), draft_id=minted.id)

    assert promoted is None
    assert sources.all() == ()
    with rows(database) as cursor:
        stored = cursor.execute(
            "SELECT encrypted_private_key FROM source_key_drafts WHERE id = ?",
            (minted.id,),
        ).fetchone()
    assert stored == (corrupted,)


def test_a_failed_draft_promotion_keeps_the_draft_and_creates_no_source(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)
    assert sources.add(a_write()) is not None
    drafts = a_key_drafts_store(database)
    minted = drafts.mint(_OWNER.id, at=_PUSHED_AT)

    promoted = sources.add_from_draft(a_write(), draft_id=minted.id)

    assert promoted is None
    assert drafts.bind(minted.id, owner_id=_OWNER.id) == minted
    assert len(sources.all()) == 1


def test_sweeping_deletes_only_drafts_older_than_the_given_moment(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    drafts = a_key_drafts_store(database)
    stale = drafts.mint(_OWNER.id, at=_PUSHED_AT)
    fresh = drafts.mint("another-admin-entirely", at=_NOTICED_GONE_AT)

    drafts.sweep(older_than=_NOTICED_GONE_AT)

    assert drafts.bind(stale.id, owner_id=_OWNER.id) is None
    assert drafts.bind(fresh.id, owner_id="another-admin-entirely") == fresh


def test_adding_a_source_does_not_rewrite_a_leftover_environment_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(_CREDENTIAL_VARIABLE, _WHAT_THE_GIT_HOST_EXPECTS)
    database = an_instance_that_was_set_up(tmp_path)
    leftover = a_leftover_environment_source(database)
    sources = a_source_store(database)

    added = sources.add(a_write())

    assert added is not None
    with rows(database) as cursor:
        credential = cursor.execute(
            _THE_CREDENTIAL_COLUMN,
            (leftover.id,),
        ).fetchone()[0]
        encrypted = cursor.execute(_THE_ENCRYPTED_OF, (leftover.id,)).fetchone()[0]
    kept = next(source for source in sources.all() if source.id == leftover.id)
    assert credential == _CREDENTIAL_VARIABLE
    assert encrypted is None
    assert kept.secret_location is None
    assert a_resolver(database).resolve(CredentialReference(name=leftover.id)) is None


def test_adding_a_source_with_a_name_or_url_already_stored_writes_nothing(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)
    stored = add_a_source(database)

    same_name = sources.add(a_write(name=stored.name))
    same_url = sources.add(a_write(url=stored.url))

    assert same_name is None
    assert same_url is None
    assert sources.all() == (stored,)


def test_removing_a_source_deletes_its_row_and_frees_its_name_and_url(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    sources = a_source_store(database)
    stored = add_a_source(database)

    sources.remove(stored.id)
    re_added = sources.add(a_write(name=stored.name, url=stored.url))

    assert re_added is not None
    assert re_added.id != stored.id
    assert sources.all() == (re_added,)


def test_pushing_the_same_folder_again_leaves_one_deck_under_its_slug(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))

    store.put(a_deck(title="Kundenfeedback Q3", commit=_A_LATER_COMMIT))

    kept = a_deck(title="Kundenfeedback Q3", commit=_A_LATER_COMMIT)
    assert store.all() == (kept,)
    assert store.get(kept.slug) == kept
    assert store.get("never-pushed") is None


def test_a_deck_whose_folder_vanished_is_gone_from_the_list_until_it_returns(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))
    store.put(a_deck("knowledge-fabric"))

    store.mark_removed_except(
        present=frozenset({"knowledge-fabric"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )
    while_it_was_gone = store.all()
    store.put(a_deck("kundenfeedback", title="Kundenfeedback Q4"))
    store.mark_removed_except(
        present=frozenset({"knowledge-fabric", "kundenfeedback"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert [deck.slug for deck in while_it_was_gone] == ["knowledge-fabric"]
    assert sorted(store.all(), key=lambda deck: deck.slug) == [
        a_deck("knowledge-fabric"),
        a_deck("kundenfeedback", title="Kundenfeedback Q4"),
    ]


def test_a_removed_decks_own_page_is_unreachable_by_slug(tmp_path: Path) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))

    store.mark_removed_except(
        present=frozenset(),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert store.get("kundenfeedback") is None


def test_a_slug_carrying_an_apostrophe_and_a_non_ascii_letter_survives_reconciliation(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    kept_slug = "l'équipe"
    store.put(a_deck(kept_slug))
    store.put(a_deck("gone"))

    store.mark_removed_except(
        present=frozenset({kept_slug}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert store.get(kept_slug) == a_deck(kept_slug)
    assert store.get("gone") is None


def test_everything_a_build_wrote_switches_over_together(tmp_path: Path) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    first = a_build(tmp_path)

    store.put_build("kundenfeedback", first)
    after_the_first = store.get("kundenfeedback")
    store.put_build("kundenfeedback", a_build(tmp_path, commit=_A_LATER_COMMIT))
    after_the_second = store.get("kundenfeedback")

    assert after_the_first is not None
    assert after_the_first.build == first
    assert after_the_second is not None
    assert after_the_second.build == a_build(tmp_path, commit=_A_LATER_COMMIT)


@pytest.mark.parametrize(
    "attempt",
    [an_attempt(outcome=BuildOutcome.RUNNING, failure=None), an_attempt()],
    ids=["a build that began", "a build that failed"],
)
def test_the_build_a_deck_last_started_is_kept_beside_the_talk_that_stands(
    tmp_path: Path,
    attempt: BuildAttempt,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck())
    built = a_build(tmp_path)
    store.put_build("kundenfeedback", built)

    store.put_attempt("kundenfeedback", attempt)

    kept = store.get("kundenfeedback")
    assert kept is not None
    assert kept.attempt == attempt
    assert kept.build == built


def test_a_build_that_switched_over_clears_the_attempt_that_led_to_it(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck())
    store.put_attempt("kundenfeedback", an_attempt())

    store.put_build("kundenfeedback", a_build(tmp_path, commit=_A_LATER_COMMIT))

    kept = store.get("kundenfeedback")
    assert kept is not None
    assert kept.attempt is None
    assert kept.build == a_build(tmp_path, commit=_A_LATER_COMMIT)


def test_a_database_written_before_attempts_records_one(tmp_path: Path) -> None:
    database = a_database_written_before_sources(tmp_path)

    create_deck_tables(database)
    with rows(database) as cursor:
        cursor.execute(
            "UPDATE decks SET source_id = ? WHERE slug = ?",
            (_SOURCE_ID, "kundenfeedback"),
        )
    store = SqliteDeckStore(database=database)
    store.put_attempt("kundenfeedback", an_attempt())

    kept = store.get("kundenfeedback")
    assert kept is not None
    assert kept.attempt == an_attempt()


def test_taking_a_deck_in_again_leaves_what_it_delivers_standing(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="Kundenfeedback"))
    built = a_build(tmp_path)
    store.put_build("kundenfeedback", built)
    standing_attempt = an_attempt()
    store.put_attempt("kundenfeedback", standing_attempt)

    store.put(a_deck(title="Kundenfeedback Q3"))
    kept = store.get("kundenfeedback")

    assert kept is not None
    assert kept.build == built
    assert kept.attempt == standing_attempt


def test_an_instance_with_no_source_row_stores_none(tmp_path: Path) -> None:
    database = an_instance_that_was_set_up(tmp_path)

    assert a_source_store(database).all() == ()


def test_a_database_written_before_sources_keeps_its_deck_row(
    tmp_path: Path,
) -> None:
    database = a_database_written_before_sources(tmp_path)

    create_deck_tables(database)

    with rows(database) as cursor:
        found = cursor.execute(
            "SELECT slug, title FROM decks WHERE slug = ?",
            ("kundenfeedback",),
        ).fetchone()
    assert found == ("kundenfeedback", "Kundenfeedback")
    assert SqliteDeckStore(database=database).all() == ()


def test_a_folder_name_another_source_carries_is_not_taken_over(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="The first"))

    taken_over = store.put(
        a_deck(title="The second", source_id=_ANOTHER_SOURCE_ID),
    )

    assert taken_over is False
    assert store.all() == (a_deck(title="The first"),)


def test_a_removed_slug_stays_with_its_source_and_is_the_deck_it_was(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck(title="The first"))
    store.mark_removed_except(
        present=frozenset(),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    taken_over = store.put(a_deck(title="The second", source_id=_ANOTHER_SOURCE_ID))
    while_it_was_gone = store.all()

    store.put(a_deck(title="The first"))
    store.mark_removed_except(
        present=frozenset({"kundenfeedback"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert taken_over is False
    assert while_it_was_gone == ()
    assert store.all() == (a_deck(title="The first"),)


def test_one_sources_reconciliation_leaves_another_sources_decks_alone(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))
    store.put(a_deck("knowledge-fabric", source_id=_ANOTHER_SOURCE_ID))

    store.mark_removed_except(
        present=frozenset(),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    assert store.all() == (a_deck("knowledge-fabric", source_id=_ANOTHER_SOURCE_ID),)


def test_removing_a_sources_decks_takes_every_row_marked_removed_or_not(
    tmp_path: Path,
) -> None:
    store = a_deck_store(tmp_path)
    store.put(a_deck("kundenfeedback"))
    store.put(a_deck("knowledge-fabric"))
    store.put(a_deck("agenten-fabrik", source_id=_ANOTHER_SOURCE_ID))
    store.mark_removed_except(
        present=frozenset({"kundenfeedback"}),
        source_id=_SOURCE_ID,
        at=_NOTICED_GONE_AT,
    )

    carried = store.for_source(_SOURCE_ID)
    store.remove_for_source(_SOURCE_ID)

    assert {deck.slug for deck in carried} == {"kundenfeedback", "knowledge-fabric"}
    assert store.all() == (a_deck("agenten-fabrik", source_id=_ANOTHER_SOURCE_ID),)
    with rows(tmp_path / "presentator.sqlite3") as cursor:
        left = cursor.execute(
            "SELECT COUNT(*) FROM decks WHERE source_id = ?",
            (_SOURCE_ID,),
        ).fetchone()
    assert left == (0,)


def test_a_source_with_no_run_yet_reads_as_nothing(tmp_path: Path) -> None:
    store = a_source_run_store(tmp_path)

    assert store.newest(_SOURCE_ID) is None


def test_a_recorded_run_is_read_back_carrying_its_commit(tmp_path: Path) -> None:
    store = a_source_run_store(tmp_path)
    run = a_successful_run(at=_PUSHED_AT)

    store.record(run)

    assert store.newest(_SOURCE_ID) == run


def test_a_recorded_failure_is_read_back_naming_why(tmp_path: Path) -> None:
    store = a_source_run_store(tmp_path)
    run = a_failed_run(at=_PUSHED_AT)

    store.record(run)

    read = store.newest(_SOURCE_ID)
    assert read == run
    assert read is not None
    assert read.commit is None
    assert read.reason is SourceRunFailure.UNREACHABLE


def test_the_newest_run_of_a_source_belongs_to_that_source_alone(
    tmp_path: Path,
) -> None:
    store = a_source_run_store(tmp_path)
    store.record(a_successful_run(at=_PUSHED_AT))
    store.record(a_failed_run(at=_NOTICED_GONE_AT))
    store.record(a_successful_run(source_id=_ANOTHER_SOURCE_ID, at=_PUSHED_AT))

    newest_of_first = store.newest(_SOURCE_ID)
    newest_of_second = store.newest(_ANOTHER_SOURCE_ID)

    assert newest_of_first == a_failed_run(at=_NOTICED_GONE_AT)
    assert newest_of_second == a_successful_run(
        source_id=_ANOTHER_SOURCE_ID,
        at=_PUSHED_AT,
    )


def test_recording_a_run_keeps_only_the_newest_bound(tmp_path: Path) -> None:
    store = a_source_run_store(tmp_path)
    for index in range(RECENT_SOURCE_RUNS + 2):
        store.record(
            a_successful_run(at=_PUSHED_AT + timedelta(minutes=index)),
        )

    recent = store.recent(_SOURCE_ID)
    with rows(tmp_path / "presentator.sqlite3") as cursor:
        kept = cursor.execute(
            "SELECT COUNT(*) FROM source_runs WHERE source_id = ?",
            (_SOURCE_ID,),
        ).fetchone()

    assert len(recent) == RECENT_SOURCE_RUNS
    assert recent[0].at == _PUSHED_AT + timedelta(minutes=RECENT_SOURCE_RUNS + 1)
    assert kept == (RECENT_SOURCE_RUNS,)


def test_removing_a_sources_runs_deletes_every_one_it_recorded(
    tmp_path: Path,
) -> None:
    store = a_source_run_store(tmp_path)
    store.record(a_successful_run(at=_PUSHED_AT))
    store.record(a_successful_run(source_id=_ANOTHER_SOURCE_ID, at=_PUSHED_AT))

    store.remove_for_source(_SOURCE_ID)

    assert store.recent(_SOURCE_ID) == ()
    assert store.recent(_ANOTHER_SOURCE_ID) != ()


def test_a_leftover_environment_row_does_not_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(_CREDENTIAL_VARIABLE, _WHAT_THE_GIT_HOST_EXPECTS)
    remote.commit_example_deck(at=_PUSHED_AT)
    database = an_instance_that_was_set_up(tmp_path)
    leftover = a_leftover_environment_source(database, url=remote.url)

    poll = folders_under(tmp_path, credentials=a_resolver(database)).folders(leftover)

    assert leftover.secret_location is None
    assert poll.folders is None
    assert poll.failure is SourceRunFailure.CREDENTIAL_UNRESOLVABLE


def test_renewing_the_access_secret_on_a_leftover_row_stores_ciphertext(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    leftover = a_leftover_environment_source(database)
    sources = a_source_store(database)

    sources.put_credential(leftover.id, _WHAT_THE_GIT_HOST_EXPECTS)
    with rows(database) as cursor:
        old_ciphertext = cursor.execute(
            _THE_ENCRYPTED_OF,
            (leftover.id,),
        ).fetchone()[0]

    replacement = "another read-only secret"
    sources.put_credential(leftover.id, replacement)

    assert sources.all()[0].secret_location is SecretLocation.STORED
    with rows(database) as cursor:
        new_ciphertext = cursor.execute(
            _THE_ENCRYPTED_OF,
            (leftover.id,),
        ).fetchone()[0]
        named = cursor.execute(_THE_CREDENTIAL_COLUMN, (leftover.id,)).fetchone()[0]
    assert old_ciphertext is not None
    assert new_ciphertext != old_ciphertext
    assert _WHAT_THE_GIT_HOST_EXPECTS.encode() not in new_ciphertext
    assert replacement.encode() not in new_ciphertext
    assert named is None
    assert (
        a_resolver(database).resolve(CredentialReference(name=leftover.id))
        == replacement
    )


def test_a_pre_change_file_whose_row_only_names_an_environment_variable_stays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(_CREDENTIAL_VARIABLE, _WHAT_THE_GIT_HOST_EXPECTS)
    database = tmp_path / "presentator.sqlite3"
    create_identity_tables(database)
    SqliteUserStore(database).add_first_account(
        Account(
            id=_OWNER.id,
            username=_OWNER.username,
            role=_OWNER.role,
            password_hash=_A_STORED_HASH,
        ),
    )
    apply_schema(database, _SOURCES_BEFORE_ENCRYPTION)
    with rows(database) as cursor:
        cursor.execute(
            _A_SOURCE_BEFORE_ENCRYPTION,
            (
                _SOURCE_ID,
                _SOURCE_NAME,
                _ADDRESS,
                MAIN_BRANCH,
                _CREDENTIAL_VARIABLE,
                _OWNER.id,
            ),
        )

    create_deck_tables(database)
    leftover = a_source_store(database).all()[0]

    assert leftover.id == _SOURCE_ID
    assert leftover.secret_location is None
    assert a_resolver(database).resolve(CredentialReference(name=leftover.id)) is None


def test_replacing_the_webhook_hash_leaves_the_previous_digest_behind(
    tmp_path: Path,
) -> None:
    database = an_instance_that_was_set_up(tmp_path)
    stored = add_a_source(database, a_write(name=_SOURCE_NAME, url=_ADDRESS))
    sources = a_source_store(database)
    replacement = b"\x22" * 32

    assert sources.put_hook_secret_hash(stored.name, replacement)
    assert sources.hook_secret_hash(stored.name) == replacement
    assert sources.hook_secret_hash("no-such-source") is None
    assert not sources.put_hook_secret_hash("no-such-source", replacement)
