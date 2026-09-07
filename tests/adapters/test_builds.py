"""Building a deck with a toolchain on PATH, against a real bare repository.

The toolchain here is a script this test wrote, not Slidev: what belongs to
this adapter is which command it runs, in which environment, under which bound,
and where the result is allowed to stand. That Slidev itself builds a deck is
proven by driving the real interface, and by the frontend job's example build.
"""

import os
import shutil
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from presentator.adapters.builds import SlidevBuilds
from presentator.adapters.decks import EnvironmentCredentials, SourceMirrors
from presentator.contracts.decks import (
    FAILURE_TEXT_LIMIT,
    Artefacts,
    BuildFailure,
    Deck,
    Source,
    talk_address,
)
from tests.conftest import EXAMPLE_SLUG, MAIN_BRANCH, GitRemote

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_A_GENEROUS_BOUND = timedelta(seconds=30)
_NO_BUDGET_AT_ALL = timedelta(0)
_OWNER = "the-admin"
_SOURCE_ID = "the-source-that-carried-it"
_A_SERVER_SECRET = "PRESENTATOR_" + "A_VALUE_ONLY_THIS_TEST_SET"
_WHAT_THE_SERVER_HOLDS = "the words only this test set in the environment"
# What the toolchain stand-in writes down, so a test can read the command line
# and the environment back.
_RECORDED_COMMAND = "command"
_RECORDED_ENVIRONMENT = "environment"
_A_BUILT_PAGE = "index.html"
_TOOLCHAIN_PROGRAM = "pnpm"
_WHAT_A_TALK_SAYS = "a talk"

_A_TOOLCHAIN_THAT_BUILDS = """#!/bin/sh
set -eu
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
case "$out" in
    *.pdf) printf '%%PDF-1.7' > "$out";;
    *) mkdir -p "$out"; printf 'a talk' > "$out/index.html";;
esac
"""
# Where to write is baked in rather than read from the environment, because
# the environment a build runs in is exactly what this stand-in is here to show.
_A_TOOLCHAIN_THAT_RECORDS = """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "{recorded}/command"
env | sort >> "{recorded}/environment"
"""
_A_TOOLCHAIN_THAT_FAILS = """#!/bin/sh
echo "the deck does not build" >&2
exit 1
"""
_WHAT_A_FAILING_TOOLCHAIN_SAID = "the deck does not build"
# A deck's own toolchain can print without limit; what a page shows is the end
# of it, where the reason stands.
_LAST_WORDS = "the last words of the toolchain"
_A_TOOLCHAIN_THAT_NEVER_STOPS_TALKING = f"""#!/bin/sh
i=0
while [ $i -lt 300 ]; do
    echo "0123456789012345678901234567890123456789" >&2
    i=$((i+1))
done
echo "{_LAST_WORDS}" >&2
exit 1
"""
_A_TOOLCHAIN_THAT_NEVER_ENDS = """#!/bin/sh
sleep 60
"""
# A background child of its own, the way slidev export's own Chromium is a
# child of slidev rather than of pnpm: only a kill of the whole group reaches
# it, so the pid it wrote down is this test's proof.
_A_TOOLCHAIN_THAT_LEAVES_A_CHILD_RUNNING = """#!/bin/sh
sleep 60 &
echo $! > "{pid_file}"
sleep 60
"""
_A_BOUND_A_SHELL_STARTS_WELL_WITHIN = timedelta(milliseconds=300)
_GRANDCHILD_GONE_WITHIN = timedelta(seconds=5)
_POLL_EVERY = timedelta(milliseconds=10)


@pytest.fixture
def source(remote: GitRemote) -> Source:
    """The one source, carrying the example deck at its newest commit."""
    remote.commit_example_deck(at=_PUSHED_AT)
    return Source(
        id=_SOURCE_ID,
        name="decks",
        url=remote.url,
        ref=MAIN_BRANCH,
        secret_location=None,
        owner_id=_OWNER,
    )


def a_deck(remote: GitRemote, *, slug: str = EXAMPLE_SLUG) -> Deck:
    return Deck(
        slug=slug,
        title="Hello Co-Presenter",
        changed_at=_PUSHED_AT,
        owner_id=_OWNER,
        source_id=_SOURCE_ID,
        commit=remote.head,
        build=None,
        attempt=None,
    )


def builds_under(
    tmp_path: Path,
    *,
    build_timeout: timedelta = _A_GENEROUS_BOUND,
) -> SlidevBuilds:
    working = tmp_path / "frontend"
    working.mkdir(exist_ok=True)
    return SlidevBuilds(
        builds=tmp_path / "builds",
        toolchain=working,
        mirrors=SourceMirrors(
            directory=tmp_path / "mirrors",
            credentials=EnvironmentCredentials(),
            pull_timeout=_A_GENEROUS_BOUND,
        ),
        build_timeout=build_timeout,
    )


def builds_ready_for(
    tmp_path: Path,
    source: Source,
    *,
    build_timeout: timedelta = _A_GENEROUS_BOUND,
) -> SlidevBuilds:
    """The adapter with the source already mirrored, as a take-in leaves it."""
    builds = builds_under(tmp_path, build_timeout=build_timeout)
    assert builds.mirrors.of(source).connect().revision is not None
    return builds


def what_it_built(built: Artefacts | BuildFailure) -> Artefacts:
    """What the build left, for a test that has already said it expects a talk."""
    assert isinstance(built, Artefacts)
    return built


def a_path_carrying(*directories: Path) -> str:
    """A PATH with git on it, because reading a deck is still spawning git."""
    git = shutil.which("git")
    assert git is not None
    beside_git = Path(git).parent
    return os.pathsep.join(str(directory) for directory in (*directories, beside_git))


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A machine whose environment holds a secret no build may ever see."""
    monkeypatch.setenv(_A_SERVER_SECRET, _WHAT_THE_SERVER_HOLDS)
    return monkeypatch


def with_the_toolchain(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    script: str,
) -> None:
    """Put a `pnpm` this test wrote on PATH, ahead of any real one."""
    somewhere_on_path = tmp_path / "toolchain-on-path"
    somewhere_on_path.mkdir(exist_ok=True)
    stand_in = somewhere_on_path / _TOOLCHAIN_PROGRAM
    stand_in.write_text(script, encoding="utf-8")
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IEXEC)
    machine.setenv("PATH", a_path_carrying(somewhere_on_path))


def recording(machine: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A toolchain that writes down the command line and environment it got."""
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    with_the_toolchain(
        machine,
        tmp_path,
        _A_TOOLCHAIN_THAT_RECORDS.format(recorded=recorded),
    )
    return recorded


def test_a_decks_folder_at_its_commit_is_built_into_a_talk_and_a_pdf(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS)
    builds = builds_ready_for(tmp_path, source)

    artefacts = what_it_built(builds.build(a_deck(remote), source=source))

    assert builds.holds(artefacts)
    assert (artefacts.directory / _A_BUILT_PAGE).read_text(
        encoding="utf-8",
    ) == _WHAT_A_TALK_SAYS
    assert artefacts.pdf.read_bytes().startswith(b"%PDF")
    assert artefacts.directory.is_relative_to(tmp_path / "builds" / EXAMPLE_SLUG)


def test_a_second_build_of_the_same_deck_leaves_the_first_one_where_it_stands(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS)
    builds = builds_ready_for(tmp_path, source)

    first = what_it_built(builds.build(a_deck(remote), source=source))
    second = what_it_built(builds.build(a_deck(remote), source=source))

    assert first.directory != second.directory
    assert builds.holds(first)
    assert builds.holds(second)


def test_the_talk_is_built_against_the_address_it_is_delivered_under(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorded = recording(machine, tmp_path)

    builds_ready_for(tmp_path, source).build(a_deck(remote), source=source)

    ran = (recorded / _RECORDED_COMMAND).read_text(encoding="utf-8").splitlines()
    assert ran[0].startswith("exec slidev build ")
    assert f"--base {talk_address(EXAMPLE_SLUG)}" in ran[0]
    assert ran[1].startswith("exec slidev export ")


def test_a_build_carries_nothing_of_the_environment_this_server_runs_in(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorded = recording(machine, tmp_path)

    builds_ready_for(tmp_path, source).build(a_deck(remote), source=source)

    carried = (recorded / _RECORDED_ENVIRONMENT).read_text(encoding="utf-8")
    assert _A_SERVER_SECRET not in carried
    assert _WHAT_THE_SERVER_HOLDS not in carried
    assert "PATH=" in carried


@pytest.mark.parametrize(
    "toolchain",
    [
        (_A_TOOLCHAIN_THAT_FAILS, _A_GENEROUS_BOUND, _WHAT_A_FAILING_TOOLCHAIN_SAID),
        (_A_TOOLCHAIN_THAT_NEVER_ENDS, _NO_BUDGET_AT_ALL, None),
    ],
    ids=["a deck that does not build", "a build that runs past its bound"],
)
def test_a_build_that_did_not_finish_says_why_and_leaves_nothing_to_point_at(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    toolchain: tuple[str, timedelta, str | None],
) -> None:
    script, build_timeout, said = toolchain
    with_the_toolchain(machine, tmp_path, script)

    builds = builds_ready_for(tmp_path, source, build_timeout=build_timeout)
    built = builds.build(a_deck(remote), source=source)

    assert built == BuildFailure(text=said)
    assert list((builds.builds / EXAMPLE_SLUG).iterdir()) == []


def test_a_toolchain_that_printed_without_end_hands_back_its_last_words(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_NEVER_STOPS_TALKING)

    built = builds_ready_for(tmp_path, source).build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    assert built.text is not None
    assert len(built.text) == FAILURE_TEXT_LIMIT
    assert built.text.endswith(_LAST_WORDS)


def test_a_build_past_its_bound_takes_the_whole_toolchain_tree_down_with_it(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    with_the_toolchain(
        machine,
        tmp_path,
        _A_TOOLCHAIN_THAT_LEAVES_A_CHILD_RUNNING.format(pid_file=pid_file),
    )
    builds = builds_ready_for(
        tmp_path,
        source,
        build_timeout=_A_BOUND_A_SHELL_STARTS_WELL_WITHIN,
    )

    built = builds.build(a_deck(remote), source=source)

    assert built == BuildFailure(text=None)
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    assert _gone_within(grandchild, deadline=_GRANDCHILD_GONE_WITHIN)


def _gone_within(pid: int, *, deadline: timedelta) -> bool:
    """Poll for the process to disappear, rather than trust a fixed wait."""
    ends_by = time.monotonic() + deadline.total_seconds()
    while time.monotonic() < ends_by:
        if not _is_alive(pid):
            return True
        time.sleep(_POLL_EVERY.total_seconds())
    return not _is_alive(pid)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_without_a_toolchain_on_the_machine_there_is_no_build(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    builds = builds_ready_for(tmp_path, source)
    deck = a_deck(remote)
    machine.setenv("PATH", a_path_carrying())

    built = builds.build(deck, source=source)

    # What the operating system says names paths of this host, so it stays in
    # the log; the page is told that this build left no words.
    assert built == BuildFailure(text=None)
    assert EXAMPLE_SLUG in caplog.text
    assert _TOOLCHAIN_PROGRAM in caplog.text


def test_a_commit_the_mirror_cannot_read_the_deck_at_builds_nothing(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS)

    built = builds_ready_for(tmp_path, source).build(
        a_deck(remote, slug="never-pushed"),
        source=source,
    )

    # A tree this host could not read out of its own mirror is nothing a deck's
    # author could act on, so the page is told nothing and the log holds it.
    assert built == BuildFailure(text=None)
    assert "never-pushed" in caplog.text


@pytest.mark.parametrize(
    "reached",
    ["outside", "through-a-link"],
    ids=["a place outside the root", "a link out of the root"],
)
def test_artefacts_that_do_not_stand_under_the_root_are_not_held(
    tmp_path: Path,
    reached: str,
) -> None:
    builds = builds_under(tmp_path)
    builds.builds.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "deck.pdf").write_bytes(b"%PDF-1.7")
    (builds.builds / "through-a-link").symlink_to(outside)
    written = tmp_path / reached if reached == "outside" else builds.builds / reached

    assert not builds.holds(Artefacts(directory=written, pdf=written / "deck.pdf"))


def test_artefacts_that_were_never_written_are_not_held(tmp_path: Path) -> None:
    builds = builds_under(tmp_path)
    never_written = builds.builds / EXAMPLE_SLUG / "talk"

    assert not builds.holds(
        Artefacts(directory=never_written, pdf=never_written / "deck.pdf"),
    )
