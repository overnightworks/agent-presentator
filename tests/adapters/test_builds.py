"""Building a deck with a toolchain on PATH, against a real bare repository.

The toolchain here is a script this test wrote, not Slidev, and the container
runner's `docker` is one too: what belongs to this adapter is which command it
runs, in which environment, under which bound, and where the result is allowed
to stand. That Slidev itself builds a deck, and that the container really
denies it this machine, is proven by driving the real interface.
"""

import os
import stat
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from errno import ELOOP
from pathlib import Path

import pytest

from gitmirror.model import CredentialReference
from presentator.adapters import builds as builds_module
from presentator.adapters.builds import (
    ContainerToolchain,
    DaemonRefusedError,
    DeckToolchain,
    HostToolchain,
    SlidevBuilds,
    the_daemon_of_this_machine,
)
from presentator.adapters.decks import SourceMirrors
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
_A_TALK_OF_TWO_FILES = 2
_WATCHED_OFTEN = timedelta(milliseconds=50)
_CLOSED_TO_EVERYONE = 0o000
_OWNER_MAY_ENTER = 0o700
# Every process a container may hold, and the two mount points a build gets.
_PROCESSES_AT_MOST = 512
_DECK_IN = "deck"
_OUT = "out"
_TALK = "talk"
_PDF_FILE = "deck.pdf"
_TOOLCHAIN_SCRATCH = "node_modules"
_A_SLUG_WITH_PUNCTUATION = "a,deck:of-punctuation"
_A_SLUG_OF_SPACES = "a deck of spaces"
# What a command line may carry beside a space: nothing here reads any of it as
# grammar, so nothing here may lose it.
_ARGUMENTS_OF_EVERY_KIND = (
    "--flag=a value",
    'quoted "like this"',
    "--network=none --cap-add=ALL",
)
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


@dataclass(frozen=True, slots=True)
class OpenCredentials:
    """A stand-in so a local bare remote can be pulled without a stored secret."""

    def resolve(self, reference: CredentialReference) -> str | None:
        return "unused-on-a-file-url"


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
# Every argument is kept whole, ended by a nul, and every command by a record
# separator: a stand-in that joined them with spaces would read two arguments
# and one argument carrying a space as the same thing.
_A_TOOLCHAIN_THAT_RECORDS = """#!/bin/sh
set -eu
for given in "$@"; do
    printf '%s\\000' "$given" >> "{recorded}/command"
done
printf '\\036' >> "{recorded}/command"
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
# A talk of the size a test asks for, and a step that goes on running after it
# wrote it: a build is stopped while it writes, not once it is done.
_A_TOOLCHAIN_THAT_WRITES = """#!/bin/sh
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
    *.pdf)
        dd if=/dev/zero of="$out" bs=$a_megabyte count={pdf} 2>/dev/null
        {and_then}
        ;;
    *)
        mkdir -p "$out"
        dd if=/dev/zero of="$out/index.html" bs=$a_megabyte count={talk} 2>/dev/null
        ;;
esac
"""
_AND_THEN_IT_KEEPS_GOING = "sleep 60"
_AND_THEN_IT_IS_DONE = ":"
# A build that leaves a link where a file would be, pointing at something of
# this machine's that is neither its to read nor its to have counted.
_A_TOOLCHAIN_THAT_LEAVES_A_LINK_OUT = """#!/bin/sh
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
    *)
        mkdir -p "$out"
        printf 'a talk' > "$out/index.html"
        ln -s "{outside}" "$out/a-way-out"
        ;;
esac
"""
_A_TOOLCHAIN_THAT_LEAVES_A_LINK_OUT_AND_FAILS = """#!/bin/sh
set -eu
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
mkdir -p "$out"
ln -s "{outside}" "$out/a-way-out"
echo "the deck does not build" >&2
exit 1
"""
_A_WAY_OUT = "a-way-out"
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
_A_TOOLCHAIN_THAT_BUILDS_BESIDE_ITSELF = """#!/bin/sh
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
    *)
        mkdir -p "$out" "$out/../beside-it" "$out/../beside-it-too"
        printf 'a talk' > "$out/index.html"
        ;;
esac
"""
_A_TOOLCHAIN_THAT_TAKES_ITS_OWN_TALK_AWAY = """#!/bin/sh
set -eu
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
case "$out" in
    *.pdf) printf '%%PDF-1.7' > "$out"; rm -rf "$(dirname "$out")";;
    *) mkdir -p "$out"; printf 'a talk' > "$out/index.html";;
esac
"""
_A_TOOLCHAIN_THAT_TAKES_THE_WHOLE_RUN_AWAY = """#!/bin/sh
set -eu
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out|--output) out="$2"; shift 2;;
        *) shift;;
    esac
done
rm -rf "$(dirname "$(dirname "$out")")"
echo "the deck does not build" >&2
exit 1
"""
_A_TOOLCHAIN_THAT_CLOSES_WHAT_IT_BUILT = """#!/bin/sh
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
    *)
        mkdir -p "$out/closed"
        printf 'a talk' > "$out/index.html"
        chmod 000 "$out/closed"
        ;;
esac
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
# One that builds nothing and cannot say whether a container of that build is
# still on its feet, and one whose own probe container will not go away.
_A_DOCKER_THAT_CANNOT_SAY_WHAT_RUNS = """#!/bin/sh
case "$1" in
    ps) echo "the daemon is not answering" >&2; exit 1;;
    rm) exit 0;;
    *) echo "the deck does not build" >&2; exit 1;;
esac
"""
_A_DAEMON_WHOSE_PROBE_STAYS = """#!/bin/sh
case "$1" in
    version) echo "1.52";;
    info) echo "overlay2";;
    image) echo "sha256:the-image-of-this-deployment";;
    create) exit 0;;
    start) exit 0;;
    rm) echo "there is no such container" >&2; exit 1;;
esac
"""
_RECORDED_REMOVAL = "removed"
# The daemon stand-ins, for the one question this server asks before it serves.
_A_DAEMON_THAT_ANSWERS = """#!/bin/sh
case "$1" in
    version) echo "{api}";;
    info) echo "{driver}";;
    image) echo "sha256:the-image-of-this-deployment";;
    create) printf 'create\\n' >> "{tried}"; exit {size};;
    start) exit 0;;
    rm) printf 'rm\\n' >> "{tried}"; exit 0;;
    ps) ;;
esac
"""
_A_DAEMON_THAT_NEVER_ANSWERS_ABOUT_MAKING_ONE = """#!/bin/sh
case "$1" in
    version) echo "1.52";;
    info) echo "overlay2";;
    image) echo "sha256:the-image-of-this-deployment";;
    create) sleep 60;;
    rm) printf '%s\\n' "$*" >> "{removed}";;
esac
"""
_A_DAEMON_THAT_NEVER_ANSWERS_ABOUT_A_SIZE = """#!/bin/sh
case "$1" in
    version) echo "1.52";;
    info) echo "overlay2";;
    image) echo "sha256:the-image-of-this-deployment";;
    create) exit 0;;
    start) sleep 60;;
    rm) printf '%s\\n' "$*" >> "{removed}";;
esac
"""
_A_DAEMON_WITHOUT_THE_IMAGE = """#!/bin/sh
case "$1" in
    version) echo "1.52";;
    info) echo "overlay2";;
    image) echo "no such image" >&2; exit 1;;
esac
"""
_A_DAEMON_THAT_WILL_NOT_SAY = """#!/bin/sh
echo "the daemon is not running" >&2
exit 1
"""
_THE_VERSION_A_SUBPATH_NEEDS = "1.45"
_A_DAEMON_NEW_ENOUGH = "1.52"
_A_DAEMON_TOO_OLD = "1.44"
_A_DRIVER = "overlay2"
_IT_TAKES_A_SIZE = 0
_IT_TAKES_NO_SIZE = 1
_TRIED_A_SIZE = "tried-a-size"
_A_REMOVAL_NEVER_WAITS_THIS_LONG = timedelta(seconds=20)
_A_SHORT_ANSWER = timedelta(milliseconds=300)
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
            credentials=OpenCredentials(),
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

    built, exported = _every_command_in(recorded)
    assert built[:3] == ["exec", "slidev", "build"]
    assert built[built.index("--base") + 1] == talk_address(EXAMPLE_SLUG)
    assert exported[:3] == ["exec", "slidev", "export"]


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

    ran = _every_command_in(recorded)
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


def _every_command_in(recorded: Path) -> list[list[str]]:
    """Every command the stand-in was given, argument by argument as it got it."""
    written = (recorded / _RECORDED_COMMAND).read_bytes()
    return [
        [given.decode() for given in one_command.split(b"\0")[:-1]]
        for one_command in written.split(b"\036")[:-1]
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


def test_a_folder_name_carrying_spaces_stays_one_argument_of_its_own(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # What a deck is delivered under is its folder's name, whatever is in it,
    # and it reaches the toolchain as the one argument it is — which is also
    # what makes the command line above provable at all.
    remote.commit_example_deck(at=_PUSHED_AT, into=_A_SLUG_OF_SPACES)
    recorded = recording(machine, tmp_path, _CONTAINER_PROGRAM)

    builds_ready_for(tmp_path, source, toolchain=in_a_container()).build(
        a_deck(remote, slug=_A_SLUG_OF_SPACES),
        source=source,
    )

    built, _ = _every_command_in(recorded)
    assert built[built.index("--base") + 1] == talk_address(_A_SLUG_OF_SPACES)


def test_what_a_stand_in_writes_down_is_every_argument_as_it_got_it(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The command lines above are only worth what this recording is worth: two
    # arguments joined into one, or one carrying a space read as two, would
    # otherwise pass for the same thing.
    recorded = recording(machine, tmp_path, _CONTAINER_PROGRAM)

    subprocess.run([_CONTAINER_PROGRAM, *_ARGUMENTS_OF_EVERY_KIND], check=True)

    assert _every_command_in(recorded) == [list(_ARGUMENTS_OF_EVERY_KIND)]


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

    # A folder name is its owner's to choose, and Docker reads commas and
    # colons as grammar; the name of a run is this server's own.
    told_the_daemon = [
        given
        for command in _every_command_in(recorded)
        for given in command
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
    # Nothing is written until the last step, so no watch sees it: what the
    # build left is added up once more when it is over.
    with_the_toolchain(
        machine,
        tmp_path,
        _A_TOOLCHAIN_THAT_WRITES.format(
            talk=0,
            pdf=_MEGABYTES_A_GREEDY_BUILD_WRITES,
            and_then=_AND_THEN_IT_IS_DONE,
        ),
    )
    builds = builds_ready_for(
        tmp_path,
        source,
        output_megabytes=_ONLY_A_MEGABYTE_MAY_BE_LEFT,
    )

    built = builds.build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    assert built.text is not None
    assert f"{_MEGABYTES_A_GREEDY_BUILD_WRITES} MB" in built.text
    assert f"at most {_ONLY_A_MEGABYTE_MAY_BE_LEFT} MB" in built.text
    assert list(builds.builds.iterdir()) == []


def test_a_build_that_has_written_too_much_is_stopped_where_it_stands(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A machine is filled while a build runs, so the step that is still
    # running when it happens does not get to finish.
    machine.setattr(builds_module, "_ACCOUNTED_EVERY", _WATCHED_OFTEN)
    with_the_toolchain(
        machine,
        tmp_path,
        _A_TOOLCHAIN_THAT_WRITES.format(
            talk=_MEGABYTES_A_GREEDY_BUILD_WRITES,
            pdf=0,
            and_then=_AND_THEN_IT_KEEPS_GOING,
        ),
    )
    builds = builds_ready_for(
        tmp_path,
        source,
        output_megabytes=_ONLY_A_MEGABYTE_MAY_BE_LEFT,
    )

    began = time.monotonic()
    built = builds.build(a_deck(remote), source=source)

    assert time.monotonic() - began < _A_GENEROUS_BOUND.total_seconds()
    assert isinstance(built, BuildFailure)
    assert built.text is not None
    assert f"{_MEGABYTES_A_GREEDY_BUILD_WRITES} MB" in built.text
    assert list(builds.builds.iterdir()) == []


def test_a_talk_of_more_files_than_a_build_may_leave_is_refused_too(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    machine.setattr(builds_module, "_ENTRIES_AT_MOST", _A_TALK_OF_TWO_FILES)
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS_BESIDE_ITSELF)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    assert built.text is not None
    assert f"more than {_A_TALK_OF_TWO_FILES} files" in built.text
    assert list(builds.builds.iterdir()) == []


def test_a_talk_that_cannot_be_added_up_in_time_is_refused_without_words(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    machine.setattr(builds_module, "_ADDING_UP_WITHIN", _NO_BUDGET_AT_ALL)
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    # What this host could not do is nothing a deck's author could act on.
    assert built == BuildFailure(text=None)
    assert EXAMPLE_SLUG in caplog.text
    assert list(builds.builds.iterdir()) == []


def test_a_talk_holding_something_this_server_cannot_read_is_refused(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_CLOSES_WHAT_IT_BUILT)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    # An entry nobody can read is a talk nobody added up, never a talk of none.
    assert built == BuildFailure(text=None)
    assert EXAMPLE_SLUG in caplog.text
    assert list(builds.builds.iterdir()) == []


def test_a_link_out_of_the_run_is_counted_as_the_name_it_is(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside, held = _something_of_this_machines(tmp_path)
    with_the_toolchain(
        machine,
        tmp_path,
        _A_TOOLCHAIN_THAT_LEAVES_A_LINK_OUT.format(outside=outside),
    )
    builds = builds_ready_for(
        tmp_path,
        source,
        output_megabytes=_ONLY_A_MEGABYTE_MAY_BE_LEFT,
    )

    artefacts = what_it_built(builds.build(a_deck(remote), source=source))

    # What the link points at is megabytes of this machine, and none of them
    # were followed, read or counted against the talk.
    assert builds.holds(artefacts)
    assert held.exists()


def test_a_link_a_build_left_is_never_opened_when_its_leavings_go(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside, held = _something_of_this_machines(tmp_path)
    outside.chmod(_CLOSED_TO_EVERYONE)
    with_the_toolchain(
        machine,
        tmp_path,
        _A_TOOLCHAIN_THAT_LEAVES_A_LINK_OUT_AND_FAILS.format(outside=outside),
    )
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    # Taking a failed build's leavings away opens what that build closed; a
    # directory of this machine it pointed at is not that build's to be opened.
    assert isinstance(built, BuildFailure)
    assert list(builds.builds.iterdir()) == []
    assert stat.S_IMODE(outside.stat().st_mode) == _CLOSED_TO_EVERYONE
    outside.chmod(_OWNER_MAY_ENTER)
    assert held.exists()


def _something_of_this_machines(tmp_path: Path) -> tuple[Path, Path]:
    """A directory of this machine, and the megabytes it holds."""
    outside = tmp_path / "of-this-machine"
    outside.mkdir()
    held = outside / "not-a-decks-business"
    held.write_bytes(b"\0" * _MEGABYTES_A_GREEDY_BUILD_WRITES * 1024 * 1024)
    return outside, held


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


def a_daemon(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    api: str = _A_DAEMON_NEW_ENOUGH,
    size: int = _IT_TAKES_A_SIZE,
) -> Path:
    """A `docker` that answers for this machine, and writes down what it ran."""
    tried = tmp_path / _TRIED_A_SIZE
    with_docker(
        machine,
        tmp_path,
        _A_DAEMON_THAT_ANSWERS.format(
            api=api,
            driver=_A_DRIVER,
            tried=tried,
            size=size,
        ),
    )
    return tried


def test_a_daemon_that_runs_a_container_under_a_size_can_bound_a_build(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    a_daemon(machine, tmp_path)

    daemon = the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)

    assert daemon.api_version == _A_DAEMON_NEW_ENOUGH
    assert daemon.storage_driver == _A_DRIVER
    assert daemon.bounds_a_container_filesystem


def test_a_daemon_that_refuses_that_container_bounds_nothing(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Which driver takes a size is not read off its name: this machine is
    # asked to run one bounded container, and it said no.
    tried = a_daemon(machine, tmp_path, size=_IT_TAKES_NO_SIZE)

    daemon = the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)

    assert not daemon.bounds_a_container_filesystem
    # A daemon that said it made nothing is not asked to take anything down.
    assert tried.read_text(encoding="utf-8").split() == ["create"]


def test_a_container_this_machine_may_have_made_is_taken_down_all_the_same(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A daemon that made the container while the client stopped waiting is
    # the one moment a probe could be left behind, so the name this call owns
    # is taken down whatever the client heard.
    machine.setattr(builds_module, "_DAEMON_ANSWERS_WITHIN", _A_SHORT_ANSWER)
    removed = tmp_path / _RECORDED_REMOVAL
    with_docker(
        machine,
        tmp_path,
        _A_DAEMON_THAT_NEVER_ANSWERS_ABOUT_MAKING_ONE.format(removed=removed),
    )

    daemon = the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)

    assert not daemon.bounds_a_container_filesystem
    taken_down = removed.read_text(encoding="utf-8").split()
    assert taken_down[:2] == ["rm", "--force"]
    assert taken_down[2].startswith(f"presentator-a-size-{os.getpid()}-")


def test_a_deployment_that_asks_for_no_size_has_this_machine_run_nothing(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tried = a_daemon(machine, tmp_path)

    daemon = the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=None)

    assert not daemon.bounds_a_container_filesystem
    assert not tried.exists()


def test_a_machine_that_will_not_answer_about_a_size_bounds_nothing(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Asking may not become the hold-up: a machine that says nothing is a
    # machine that bounds nothing, and this instance is told so at once.
    machine.setattr(builds_module, "_DAEMON_ANSWERS_WITHIN", _A_SHORT_ANSWER)
    removed = tmp_path / _RECORDED_REMOVAL
    with_docker(
        machine,
        tmp_path,
        _A_DAEMON_THAT_NEVER_ANSWERS_ABOUT_A_SIZE.format(removed=removed),
    )

    daemon = the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)

    assert not daemon.bounds_a_container_filesystem
    # The container that was asked is taken down even where the asking never
    # came back, so a start leaves nothing of its own behind.
    taken_down = removed.read_text(encoding="utf-8").split()
    assert taken_down[:2] == ["rm", "--force"]
    assert taken_down[2].startswith("presentator-a-size-")


@pytest.mark.parametrize(
    "docker",
    [
        _A_DAEMON_THAT_ANSWERS.format(
            api=_A_DAEMON_TOO_OLD,
            driver=_A_DRIVER,
            tried="",
            size=_IT_TAKES_A_SIZE,
        ),
        _A_DAEMON_THAT_ANSWERS.format(
            api="not-a-version",
            driver=_A_DRIVER,
            tried="",
            size=_IT_TAKES_A_SIZE,
        ),
        _A_DAEMON_WITHOUT_THE_IMAGE,
        _A_DAEMON_THAT_WILL_NOT_SAY,
    ],
    ids=[
        "one too old to keep a build in its place",
        "one talking nonsense",
        "one without the image every build runs in",
        "none",
    ],
)
def test_a_daemon_no_build_may_be_trusted_to_is_refused(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    docker: str,
) -> None:
    with_docker(machine, tmp_path, docker)

    with pytest.raises(DaemonRefusedError):
        the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)


def test_without_a_client_on_the_machine_no_daemon_answers(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    nothing_on_it = tmp_path / "an-empty-path"
    nothing_on_it.mkdir()
    machine.setenv("PATH", str(nothing_on_it))

    with pytest.raises(DaemonRefusedError):
        the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)


def test_a_talk_that_is_gone_by_the_time_it_is_added_up_is_refused(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_TAKES_ITS_OWN_TALK_AWAY)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    # A build that took its own talk away left nothing to add up and nothing
    # to deliver; what was written is not guessed at.
    assert built == BuildFailure(text=None)
    assert EXAMPLE_SLUG in caplog.text


def test_a_run_that_took_itself_away_leaves_the_next_build_nothing_to_do(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_TAKES_THE_WHOLE_RUN_AWAY)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    assert isinstance(built, BuildFailure)
    assert list(builds.builds.iterdir()) == []


def test_a_name_that_is_no_longer_a_directory_when_it_is_opened_is_refused(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A build turning one of its own directories into a link between the
    # moment it is read and the moment it is opened cannot be arranged on
    # demand; what that leaves is an open that fails, and this is that.
    def a_link_by_now(name: str, *, held_by: int) -> int:
        del name, held_by
        raise OSError(ELOOP, "the name is a link now")

    machine.setattr(builds_module, "_opened_within", a_link_by_now)
    with_the_toolchain(machine, tmp_path, _A_TOOLCHAIN_THAT_BUILDS)
    builds = builds_ready_for(tmp_path, source)

    built = builds.build(a_deck(remote), source=source)

    # Refused, and never followed: nothing of this machine was added up.
    assert built == BuildFailure(text=None)
    assert EXAMPLE_SLUG in caplog.text
    assert list(builds.builds.iterdir()) == []


def test_leavings_of_a_build_that_may_still_run_are_taken_away_as_they_lie(
    remote: GitRemote,
    source: Source,
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with_docker(machine, tmp_path, _A_DOCKER_THAT_CANNOT_SAY_WHAT_RUNS)
    builds = builds_ready_for(tmp_path, source, toolchain=in_a_container())

    built = builds.build(a_deck(remote), source=source)

    # Opening a directory for a build that may still be writing in it is
    # opening it for that build, so nothing is opened and the log says so.
    assert isinstance(built, BuildFailure)
    assert "may still be running" in caplog.text
    assert list(builds.builds.iterdir()) == []


def test_a_probe_container_that_would_not_go_away_is_named_in_the_log(
    machine: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with_docker(machine, tmp_path, _A_DAEMON_WHOSE_PROBE_STAYS)

    daemon = the_daemon_of_this_machine(image=_THE_BUILD_IMAGE, disk=_A_DISK_BOUND)

    assert daemon.bounds_a_container_filesystem
    assert "presentator-a-size-" in caplog.text
