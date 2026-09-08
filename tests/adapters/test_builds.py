"""Building a deck with a toolchain on PATH, against a real bare repository.

The toolchain here is a script this test wrote, not Slidev, and the container
runner's `docker` is one too: what belongs to this adapter is which command it
runs, in which environment, under which bound, and where the result is allowed
to stand. That Slidev itself builds a deck, and that the container really
denies it this machine, is proven by driving the real interface.
"""

import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from presentator.adapters.builds import (
    ContainerToolchain,
    DaemonRefusedError,
    DeckToolchain,
    HostToolchain,
    SlidevBuilds,
    the_daemon_of_this_machine,
)
from presentator.adapters.decks import EnvironmentCredentials, SourceMirrors
from presentator.contracts.decks import (
    FAILURE_TEXT_LIMIT,
    SLIDES_FILE,
    Artefacts,
    BuildFailure,
    Deck,
    Source,
    talk_address,
)
from tests.conftest import (
    EXAMPLE_SLUG,
    MAIN_BRANCH,
    GitRemote,
    a_path_carrying,
    with_the_program,
)

_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_A_GENEROUS_BOUND = timedelta(seconds=30)
# What a deployment names: the image the toolchain stands in, the volume the
# builds root is a directory of, and what one build may take of the machine.
_THE_BUILD_IMAGE = "the-build-image-of-this-deployment"
_THE_BUILDS_VOLUME = "the-builds-volume-of-this-deployment"
_A_MEMORY_BOUND = "3g"
_A_DISK_BOUND = "5g"
# Bigger than the few bytes a stand-in writes, so only the test about the bound
# ever meets it.
_A_TALK_THIS_BIG_IS_FINE = 64
_ONLY_A_MEGABYTE_MAY_BE_LEFT = 1
_MEGABYTES_A_GREEDY_BUILD_WRITES = 3
# Every process a container may hold, and the two mount points a build gets.
_PROCESSES_AT_MOST = 512
_DECK_IN = "deck"
_OUT = "out"
_TALK = "talk"
_PDF_FILE = "deck.pdf"
_TOOLCHAIN_SCRATCH = "node_modules"
_A_SLUG_WITH_PUNCTUATION = "a,deck:of-punctuation"
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
_CONTAINER_PROGRAM = "docker"
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
# A deck's own toolchain can print megabytes; what a page shows is the end of
# it, where the reason stands, and that end is all this server ever holds.
_LAST_WORDS = "the last words of the toolchain"
_A_TOOLCHAIN_THAT_NEVER_STOPS_TALKING = f"""#!/bin/sh
i=0
while [ $i -lt 32 ]; do
    dd if=/dev/zero bs=65536 count=8 2>/dev/null | tr '\\0' 'x' >&2
    i=$((i+1))
done
echo "{_LAST_WORDS}" >&2
exit 1
"""
_A_TOOLCHAIN_THAT_NEVER_ENDS = """#!/bin/sh
sleep 60
"""
# One that says everything it has to say and then hangs anyway, the way an
# export whose browser never comes back does.
_A_TOOLCHAIN_THAT_STOPS_TALKING_BUT_NOT_RUNNING = """#!/bin/sh
exec 2>&-
sleep 60
"""
# Three megabytes of talk, for a test that allows one.
_A_TOOLCHAIN_THAT_WRITES_TOO_MUCH = """#!/bin/sh
set -eu
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
a_megabyte=1048576
case "$out" in
    *.pdf) dd if=/dev/zero of="$out" bs=$a_megabyte count=1 2>/dev/null;;
    *)
        mkdir -p "$out"
        dd if=/dev/zero of="$out/index.html" bs=$a_megabyte count=2 2>/dev/null
        ;;
esac
"""
# A deck's own code writes in the directory of its run, and may close a
# directory behind it that the run's leftovers then have to be opened out of.
# A development run gives a deck this server's own rights, so its build can
# close the very root every build stands in.
_A_TOOLCHAIN_THAT_CLOSES_THE_ROOT = """#!/bin/sh
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
mkdir -p "$out"
chmod 500 "$(dirname "$(dirname "$(dirname "$out")")")"
echo "the deck does not build" >&2
exit 1
"""
_A_TOOLCHAIN_THAT_CLOSES_WHAT_IT_WROTE = """#!/bin/sh
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
mkdir -p "$out/closed"
chmod 000 "$out/closed"
echo "the deck does not build" >&2
exit 1
"""

# The `docker` stand-ins. A step names the same paths inside a container as
# outside it, so the toolchain scripts above double as a `docker` that runs
# them; these two are about the container itself, which only a step that was
# given up on has anything to say about.
_A_DOCKER_THAT_TAKES_ITS_CONTAINER_DOWN = """#!/bin/sh
if [ "$1" = "rm" ]; then
    printf '%s\\n' "$*" >> "{recorded}/removed"
    exit 0
fi
sleep 60
"""
_A_DOCKER_WHOSE_CONTAINER_STAYS = """#!/bin/sh
if [ "$1" = "rm" ]; then
    echo "there is no such container" >&2
    exit 1
fi
sleep 60
"""
_A_DOCKER_THAT_WILL_NOT_ANSWER = """#!/bin/sh
sleep 60
"""
_RECORDED_REMOVAL = "removed"
# The daemon stand-ins, for the one question this server asks before it serves.
_A_DAEMON_THAT_ANSWERS = """#!/bin/sh
case "$1" in
    version) echo "{api}";;
    info) echo "{driver}";;
esac
"""
_A_DAEMON_THAT_WILL_NOT_SAY = """#!/bin/sh
echo "the daemon is not running" >&2
exit 1
"""
_THE_VERSION_A_SUBPATH_NEEDS = "1.45"
_A_DAEMON_NEW_ENOUGH = "1.52"
_A_DAEMON_TOO_OLD = "1.44"
_A_REMOVAL_NEVER_WAITS_THIS_LONG = timedelta(seconds=20)
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


def a_source(remote: GitRemote) -> Source:
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


@pytest.fixture
def source(remote: GitRemote) -> Source:
    """That source, for the tests that only ever want the one."""
    return a_source(remote)


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


def on_this_machine(
    tmp_path: Path,
    *,
    bound: timedelta = _A_GENEROUS_BOUND,
) -> HostToolchain:
    """The toolchain a development run builds with: a project on this machine."""
    project = tmp_path / "frontend"
    project.mkdir(exist_ok=True)
    return HostToolchain(project=project, bound=bound)


def in_a_container(
    *,
    bound: timedelta = _A_GENEROUS_BOUND,
    disk: str | None = _A_DISK_BOUND,
) -> ContainerToolchain:
    """The toolchain an instance carrying anyone's decks builds with."""
    return ContainerToolchain(
        image=_THE_BUILD_IMAGE,
        volume=_THE_BUILDS_VOLUME,
        memory=_A_MEMORY_BOUND,
        disk=disk,
        bound=bound,
    )


def builds_under(
    tmp_path: Path,
    *,
    toolchain: DeckToolchain,
    output_megabytes: int = _A_TALK_THIS_BIG_IS_FINE,
) -> SlidevBuilds:
    return SlidevBuilds(
        builds=tmp_path / "builds",
        toolchain=toolchain,
        mirrors=SourceMirrors(
            directory=tmp_path / "mirrors",
            credentials=EnvironmentCredentials(),
            pull_timeout=_A_GENEROUS_BOUND,
        ),
        output_megabytes=output_megabytes,
    )


def builds_ready_for(
    tmp_path: Path,
    source: Source,
    *,
    toolchain: DeckToolchain | None = None,
    output_megabytes: int = _A_TALK_THIS_BIG_IS_FINE,
) -> SlidevBuilds:
    """The adapter with the source already mirrored, as a take-in leaves it."""
    builds = builds_under(
        tmp_path,
        toolchain=toolchain if toolchain is not None else on_this_machine(tmp_path),
        output_megabytes=output_megabytes,
    )
    assert builds.mirrors.of(source).connect().revision is not None
    return builds


def what_it_built(built: Artefacts | BuildFailure) -> Artefacts:
    """What the build left, for a test that has already said it expects a talk."""
    assert isinstance(built, Artefacts)
    return built


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
    """Put a `pnpm` this test wrote on PATH, for a build on this machine."""
    with_the_program(machine, tmp_path, _TOOLCHAIN_PROGRAM, script)


def with_docker(machine: pytest.MonkeyPatch, tmp_path: Path, script: str) -> None:
    """Put a `docker` this test wrote on PATH, for a build in a container."""
    with_the_program(machine, tmp_path, _CONTAINER_PROGRAM, script)


def recording(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    program: str = _TOOLCHAIN_PROGRAM,
) -> Path:
    """A program that writes down the command line and environment it got."""
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    with_the_program(
        machine,
        tmp_path,
        program,
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
    assert artefacts.directory.is_relative_to(tmp_path / "builds")


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
    script, bound, said = toolchain
    with_the_toolchain(machine, tmp_path, script)

    builds = builds_ready_for(
        tmp_path, source, toolchain=on_this_machine(tmp_path, bound=bound)
    )
    built = builds.build(a_deck(remote), source=source)

    assert built == BuildFailure(text=said)
    assert list(builds.builds.iterdir()) == []


def test_a_step_that_stopped_talking_but_not_running_is_given_up_on_too(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_the_toolchain(
        machine, tmp_path, _A_TOOLCHAIN_THAT_STOPS_TALKING_BUT_NOT_RUNNING
    )
    builds = builds_ready_for(
        tmp_path,
        source,
        toolchain=on_this_machine(tmp_path, bound=_A_BOUND_A_SHELL_STARTS_WELL_WITHIN),
    )

    built = builds.build(a_deck(remote), source=source)

    assert built == BuildFailure(text=None)
    assert list(builds.builds.iterdir()) == []


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
        toolchain=on_this_machine(tmp_path, bound=_A_BOUND_A_SHELL_STARTS_WELL_WITHIN),
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
    builds = builds_under(tmp_path, toolchain=on_this_machine(tmp_path))
    builds.builds.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "deck.pdf").write_bytes(b"%PDF-1.7")
    (builds.builds / "through-a-link").symlink_to(outside)
    written = tmp_path / reached if reached == "outside" else builds.builds / reached

    assert not builds.holds(Artefacts(directory=written, pdf=written / "deck.pdf"))


def test_artefacts_that_were_never_written_are_not_held(tmp_path: Path) -> None:
    builds = builds_under(tmp_path, toolchain=on_this_machine(tmp_path))
    never_written = builds.builds / "a-run-that-never-was" / _TALK

    assert not builds.holds(
        Artefacts(directory=never_written, pdf=never_written / "deck.pdf"),
    )


def test_a_deck_built_in_a_container_stands_where_this_machine_would_stand_it(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_docker(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS)
    builds = builds_ready_for(tmp_path, source, toolchain=in_a_container())

    artefacts = what_it_built(builds.build(a_deck(remote), source=source))

    assert builds.holds(artefacts)
    assert (artefacts.directory / _A_BUILT_PAGE).read_text(
        encoding="utf-8",
    ) == _WHAT_A_TALK_SAYS
    assert artefacts.pdf.read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize(
    "disk",
    [_A_DISK_BOUND, None],
    ids=["a daemon that bounds a filesystem", "one that cannot"],
)
def test_a_build_container_is_asked_for_exactly_these_arguments_and_no_others(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    disk: str | None,
) -> None:
    recorded = recording(machine, tmp_path, _CONTAINER_PROGRAM)

    builds_ready_for(tmp_path, source, toolchain=in_a_container(disk=disk)).build(
        a_deck(remote),
        source=source,
    )

    ran = [
        line.split(" ")
        for line in (recorded / _RECORDED_COMMAND)
        .read_text(encoding="utf-8")
        .split("\n")
        if line
    ]
    run = _the_run_of(ran[0])
    deck, out = run / _DECK_IN, run / _OUT
    # The whole command line of both steps, in order: nothing this server does
    # not say is in it, so no later argument can loosen an earlier one.
    assert ran == [
        [
            *a_container_run(run, step="build", disk=disk),
            str(deck / SLIDES_FILE),
            "--base",
            talk_address(EXAMPLE_SLUG),
            "--out",
            str(out / _TALK),
        ],
        [
            *a_container_run(run, step="export", disk=disk),
            str(deck / SLIDES_FILE),
            "--output",
            str(out / _PDF_FILE),
        ],
    ]


def a_container_run(run: Path, *, step: str, disk: str | None) -> list[str]:
    """Every argument a build's container is asked for, before Slidev's own."""
    deck, out = run / _DECK_IN, run / _OUT
    held = [f"--memory={_A_MEMORY_BOUND}", f"--pids-limit={_PROCESSES_AT_MOST}"]
    if disk is not None:
        held.append(f"--storage-opt=size={disk}")
    return [
        "run",
        "--rm",
        f"--name=presentator-{step}-{run.name}",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pull=never",
        *held,
        _a_mount_of(deck, subpath=f"{run.name}/{_DECK_IN}", access=",readonly"),
        f"--tmpfs={deck / _TOOLCHAIN_SCRATCH}:mode=1777",
        _a_mount_of(out, subpath=f"{run.name}/{_OUT}", access=""),
        _THE_BUILD_IMAGE,
        "pnpm",
        "exec",
        "slidev",
        step,
    ]


def _a_mount_of(directory: Path, *, subpath: str, access: str) -> str:
    """One directory of the builds volume, at the path this server names it by."""
    return (
        f"--mount=type=volume,src={_THE_BUILDS_VOLUME}"
        f",dst={directory},volume-subpath={subpath}{access}"
    )


def _the_run_of(command: list[str]) -> Path:
    """The directory that run works in, read off the command it was given."""
    return Path(command[command.index("--out") + 1]).parent.parent


def test_the_client_speaks_only_the_version_that_keeps_a_build_in_its_own_place(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorded = recording(machine, tmp_path, _CONTAINER_PROGRAM)

    builds_ready_for(tmp_path, source, toolchain=in_a_container()).build(
        a_deck(remote),
        source=source,
    )

    carried = (recorded / _RECORDED_ENVIRONMENT).read_text(encoding="utf-8")
    # An older daemon ignores a subpath it cannot read and would mount the
    # whole volume; pinned to this version, it refuses the call instead.
    assert f"DOCKER_API_VERSION={_THE_VERSION_A_SUBPATH_NEEDS}" in carried
    assert _A_SERVER_SECRET not in carried
    assert _WHAT_THE_SERVER_HOLDS not in carried


def test_no_argument_the_daemon_is_given_carries_a_name_somebody_else_chose(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote.commit_example_deck(at=_PUSHED_AT, into=_A_SLUG_WITH_PUNCTUATION)
    recorded = recording(machine, tmp_path, _CONTAINER_PROGRAM)

    builds_ready_for(tmp_path, source, toolchain=in_a_container()).build(
        a_deck(remote, slug=_A_SLUG_WITH_PUNCTUATION),
        source=source,
    )

    ran = (recorded / _RECORDED_COMMAND).read_text(encoding="utf-8").split()
    # A folder name is its owner's to choose, and Docker reads commas and
    # colons as grammar; the name of a run is this server's own.
    told_the_daemon = [
        given
        for given in ran
        if given.startswith(("--name=", "--mount=", "--tmpfs=", "--storage-opt="))
    ]
    assert told_the_daemon
    assert not any(_A_SLUG_WITH_PUNCTUATION in given for given in told_the_daemon)


def test_a_talk_larger_than_a_build_may_leave_is_refused_with_that_reason(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_docker(machine, tmp_path, _A_TOOLCHAIN_THAT_WRITES_TOO_MUCH)
    builds = builds_ready_for(
        tmp_path,
        source,
        toolchain=in_a_container(),
        output_megabytes=_ONLY_A_MEGABYTE_MAY_BE_LEFT,
    )

    built = builds.build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    assert built.text is not None
    assert f"{_MEGABYTES_A_GREEDY_BUILD_WRITES} MB" in built.text
    assert f"at most {_ONLY_A_MEGABYTE_MAY_BE_LEFT} MB" in built.text
    assert list(builds.builds.iterdir()) == []


def test_a_build_container_past_its_bound_is_taken_down_rather_than_left_running(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    with_docker(
        machine,
        tmp_path,
        _A_DOCKER_THAT_TAKES_ITS_CONTAINER_DOWN.format(recorded=recorded),
    )
    builds = builds_ready_for(
        tmp_path,
        source,
        toolchain=in_a_container(bound=_A_BOUND_A_SHELL_STARTS_WELL_WITHIN),
    )

    built = builds.build(a_deck(remote), source=source)

    assert built == BuildFailure(text=None)
    taken_down = (recorded / _RECORDED_REMOVAL).read_text(encoding="utf-8").split()
    assert taken_down[:2] == ["rm", "--force"]
    assert list(builds.builds.iterdir()) == []


@pytest.mark.parametrize(
    "docker",
    [_A_DOCKER_WHOSE_CONTAINER_STAYS, _A_DOCKER_THAT_WILL_NOT_ANSWER],
    ids=["a daemon that refuses", "a daemon that does not answer at all"],
)
def test_a_build_container_that_could_not_be_taken_down_is_named_in_the_log(
    remote: GitRemote,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    docker: str,
) -> None:
    with_docker(machine, tmp_path, docker)
    source = a_source(remote)
    builds = builds_ready_for(
        tmp_path,
        source,
        toolchain=in_a_container(bound=_A_BOUND_A_SHELL_STARTS_WELL_WITHIN),
    )

    began = time.monotonic()
    built = builds.build(a_deck(remote), source=source)

    # A daemon that never answers may not hold every later build behind it.
    assert time.monotonic() - began < _A_REMOVAL_NEVER_WAITS_THIS_LONG.total_seconds()
    assert built == BuildFailure(text=None)
    assert EXAMPLE_SLUG in caplog.text


def test_what_a_build_closed_behind_it_is_opened_and_taken_away_all_the_same(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_CLOSES_WHAT_IT_WROTE)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    assert list(builds.builds.iterdir()) == []


def test_what_could_not_be_taken_away_is_named_in_the_log(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_CLOSES_THE_ROOT)
    builds = builds_ready_for(tmp_path, source)

    with _opened_again_afterwards(builds.builds):
        built = builds.build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    left = list(builds.builds.iterdir())
    assert len(left) == 1
    assert left[0].name in caplog.text


@contextmanager
def _opened_again_afterwards(root: Path) -> Generator[None]:
    """Whatever the build did to the root, leave it removable for the next test."""
    try:
        yield
    finally:
        root.chmod(0o700)


@pytest.mark.parametrize("driver", ["overlay2 xfs", "btrfs", "zfs"])
def test_a_daemon_on_such_a_driver_can_hold_a_build_to_a_size(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    driver: str,
) -> None:
    with_docker(
        machine,
        tmp_path,
        _A_DAEMON_THAT_ANSWERS.format(api=_A_DAEMON_NEW_ENOUGH, driver=driver),
    )

    daemon = the_daemon_of_this_machine()

    assert daemon.api_version == _A_DAEMON_NEW_ENOUGH
    assert daemon.bounds_a_container_filesystem


@pytest.mark.parametrize("driver", ["overlay2 extfs", "vfs"])
def test_a_daemon_on_any_other_driver_leaves_a_build_unbounded_there(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    driver: str,
) -> None:
    with_docker(
        machine,
        tmp_path,
        _A_DAEMON_THAT_ANSWERS.format(api=_A_DAEMON_NEW_ENOUGH, driver=driver),
    )

    assert not the_daemon_of_this_machine().bounds_a_container_filesystem


@pytest.mark.parametrize(
    "docker",
    [
        _A_DAEMON_THAT_ANSWERS.format(api=_A_DAEMON_TOO_OLD, driver="overlay2 xfs"),
        _A_DAEMON_THAT_ANSWERS.format(api="not-a-version", driver="overlay2 xfs"),
        _A_DAEMON_THAT_WILL_NOT_SAY,
    ],
    ids=["one too old to keep a build in its place", "one talking nonsense", "none"],
)
def test_a_daemon_no_build_may_be_trusted_to_is_refused(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    docker: str,
) -> None:
    with_docker(machine, tmp_path, docker)

    with pytest.raises(DaemonRefusedError):
        the_daemon_of_this_machine()


def test_without_a_client_on_the_machine_no_daemon_answers(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    nothing_on_it = tmp_path / "an-empty-path"
    nothing_on_it.mkdir()
    machine.setenv("PATH", str(nothing_on_it))

    with pytest.raises(DaemonRefusedError):
        the_daemon_of_this_machine()
