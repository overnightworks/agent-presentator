"""Turning a deck's folder into the talk it delivers, with Slidev (ADR 0005).

Slidev's own command line owns the build and the PDF export, so this adapter
spawns it rather than binding a second renderer. The deck's tree is written out
of the bare mirror beside it, because a toolchain needs files.

**A deck is code.** Where that code runs is this module's second question. A
container of the build's own answers it (line 14a): no network, nothing of this
filesystem but the deck itself and the directory this run produces, and no
environment of this server's. Running the toolchain on this host instead is the
development answer, and there a deck's Vue components run with this process's
rights.

What a build may take is bounded on every side it has: the time it may run, the
memory and processes its container may use, what it may write into its own
filesystem and into its talk, and how much of what it prints this server ever
holds.
"""

import logging
import os
import select
import shutil
import signal
import subprocess
from abc import abstractmethod
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from typing import IO, Final, Protocol

from gitmirror.model import MirrorError, Revision
from presentator.adapters.decks import SourceMirrors
from presentator.contracts.decks import (
    FAILURE_TEXT_LIMIT,
    SLIDES_FILE,
    Artefacts,
    BuildFailure,
    Deck,
    Source,
    bounded_failure,
    talk_address,
)

_TOOLCHAIN: Final = "pnpm"
_SLIDEV: Final = "slidev"
_DOCKER: Final = "docker"
_BUILD: Final = "build"
_EXPORT: Final = "export"
# The three directories one run of the build owns: the deck's own tree as the
# toolchain reads it, everything that run writes, and the talk inside that.
_DECK_IN: Final = "deck"
_OUT: Final = "out"
_TALK: Final = "talk"
_PDF_FILE: Final = "deck.pdf"
# Only what a Node toolchain needs to run; a step inherits nothing else, so
# neither a source's read-only secret nor this instance's key is in reach of a
# deck's own build-time code.
_INHERITED: Final = ("PATH", "HOME")
# Mounting one directory of a volume is what keeps a build inside its own run.
# A daemon older than this ignores the subpath it cannot read and mounts the
# whole volume — every deck's talk — into a deck's own container, so the client
# is pinned to this version: an older daemon then refuses the call instead of
# doing something wider than it was asked. A start against one is refused too.
_MOUNT_SUBPATH_SINCE: Final = (1, 45)
_PINNED_API: Final = "DOCKER_API_VERSION"
_AS_A_VERSION: Final = ".".join(str(part) for part in _MOUNT_SUBPATH_SINCE)
# What a build's container may not do, whatever a deck's own code tries: reach
# any network, gain a capability, or take a privilege from a setuid program.
# The image runs the toolchain as its own unprivileged user, so nothing here
# names one, and the filesystem it writes on is the image's own, thrown away
# with the container. A missing image is a fault of this machine rather than a
# reason to fetch one.
_SEALED: Final = (
    "--network=none",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pull=never",
)
# Slidev writes its virtual modules into the deck it is reading, so that one
# directory is a filesystem of its own that nothing keeps: the deck's tree
# stays as git carried it, and the memory bound covers what a build writes
# there, because a tmpfs page is charged to the container that wrote it. Docker
# mounts it as root's, so the toolchain's own user needs it open — and nothing
# outside this one container can see it.
_TOOLCHAIN_SCRATCH: Final = "node_modules"
_SCRATCH_IS_EVERYONES: Final = "mode=1777"
# A build that spawns without end is a build that takes the machine down with
# it; a Slidev export and its browser stay far below this.
_PROCESSES_AT_MOST: Final = 512
_CONTAINER_PREFIX: Final = "presentator"
# Docker holds a container's own filesystem to a size on these drivers only;
# on any other it refuses the option rather than bounding anything.
_OVERLAY: Final = "overlay2"
_XFS: Final = "xfs"
_DRIVERS_THAT_BOUND_A_FILESYSTEM: Final = frozenset({"btrfs", "zfs"})
_BACKING_FILESYSTEM: Final = "Backing Filesystem"
# One line naming the driver and, where it has one, the filesystem it writes
# on; the two together decide whether a container's own writes can be bounded.
_THE_DRIVER: Final = (
    "{{.Driver}}"
    "{{range .DriverStatus}}"
    f'{{{{if eq (index . 0) "{_BACKING_FILESYSTEM}"}}}} {{{{index . 1}}}}{{{{end}}}}'
    "{{end}}"
)
# Asking the daemon what it is, and taking down a container that outlived its
# step, are calls to a socket on this machine; neither may hold the walk of
# builds for longer than this.
_DAEMON_ANSWERS_WITHIN: Final = timedelta(seconds=20)
_REMOVAL_WITHIN: Final = timedelta(seconds=30)
_READ_AT_ONCE: Final = 64 * 1024
# A page shows the end of what a build printed, so only that much of it is ever
# held: four bytes is the longest one character can be.
_TAIL_BYTES: Final = 4 * FAILURE_TEXT_LIMIT
_A_MEGABYTE: Final = 1024 * 1024
_OWNER_MAY_ENTER: Final = 0o700
_UNREADABLE_DECK: Final = "deck %s cannot be read at %s: %s"
_TOOLCHAIN_FAILED: Final = "%s of deck %s failed: %s"
_TOOLCHAIN_UNAVAILABLE: Final = "%s of deck %s could not be run: %s"
_TOOLCHAIN_TIMED_OUT: Final = "%s of deck %s ran past %s and was given up"
_CONTAINER_STAYED: Final = "the container of %s of deck %s could not be removed: %s"
_RUN_STAYED: Final = "what build %s left could not be taken away"
_TOO_MUCH_WRITTEN: Final = "deck %s wrote %s MB, which is more than a build may leave"
# What a person reads on the deck page when no toolchain failed but the build
# is refused all the same.
_TOO_MUCH_FOR_A_PAGE: Final = (
    "The talk this build wrote is {written} MB; a build may leave at most {allowed} MB."
)
_NO_CLIENT: Final = "no container client answers on this machine: {said}"
_NO_DAEMON: Final = "this machine's container daemon did not answer: {said}"
_TOO_OLD: Final = (
    "this machine's container daemon speaks {spoken},"
    f" and a build needs {_AS_A_VERSION} to be given one directory of a volume"
)

_log = logging.getLogger(__name__)


class DaemonRefusedError(RuntimeError):
    """This machine's container daemon is not one a deck may be built on."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Daemon:
    """What this machine's container daemon is, in its own client's words."""

    api_version: str
    storage_driver: str
    backing_filesystem: str

    @property
    def bounds_a_container_filesystem(self) -> bool:
        """Whether this driver can hold a container's own writes to a size.

        Docker takes a size on btrfs and zfs, and on overlay2 only over xfs
        with project quotas. Asking any other driver for one is a refusal
        rather than a bound, and a refusal would fail every build.
        """
        return self.storage_driver in _DRIVERS_THAT_BOUND_A_FILESYSTEM or (
            self.storage_driver == _OVERLAY and self.backing_filesystem == _XFS
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildPlace:
    """Where one run of the build works: the root, and its own directory in it.

    That directory is named relative to the root, because the root is the one
    place two filesystems both know: this server's, and that of a container the
    build runs in.
    """

    root: Path
    run: Path

    @property
    def here(self) -> Path:
        """That run's own directory, as this server names it."""
        return self.root / self.run


@dataclass(frozen=True, slots=True, kw_only=True)
class _HowItIsRun:
    """How one runner starts a step, bounds it, and clears up after the bound."""

    in_directory: Path | None
    environment: dict[str, str]
    bound: timedelta
    given_up: Callable[[], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildStep:
    """One Slidev command of one build, and the deck it is run for.

    The paths in the arguments are this server's own. A toolchain that runs the
    command somewhere else gives that place the very same paths, so a step's
    command line says the same thing wherever it runs.
    """

    place: BuildPlace
    slug: str
    name: str
    arguments: tuple[str, ...]


class DeckToolchain(Protocol):
    """Runs one Slidev command over the directories a build works in."""

    @abstractmethod
    def ran(self, step: BuildStep) -> BuildFailure | None:
        """Run that step, saying nothing when it did what it was asked to.

        What the toolchain printed travels back with a failure, because the
        deck's page shows it (line 9); a step that was given up on, and one
        that could not be started at all, hand back no words.
        """


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerToolchain:
    """Runs Slidev in a container of the build's own (line 14a, ADR 0005).

    A deck is code, so its build gets a machine of its own: no network, no
    capability, and none of this server's environment. What it can reach of
    this filesystem is the deck's tree, read-only, and the directory this run
    writes into — both of them directories of the volume the builds root is,
    mounted at the very paths this server names them by. Everything else it
    writes is the container's own and goes with it, within the size this
    machine's storage driver can hold it to.
    """

    image: str
    volume: str
    memory: str
    disk: str | None
    bound: timedelta

    def ran(self, step: BuildStep) -> BuildFailure | None:
        """Run that step in a container of its own, and take it down after."""
        container = _a_container_for(step)
        # Slidev writes into the deck it reads, which this container is given
        # read-only, so that one directory is a mount of its own — and a mount
        # needs a place to stand that the deck's own tree does not carry.
        scratch = step.place.here / _DECK_IN / _TOOLCHAIN_SCRATCH
        scratch.mkdir(exist_ok=True)
        return _what_a_step_said(
            (
                _DOCKER,
                "run",
                "--rm",
                f"--name={container}",
                *_SEALED,
                *self._bounds(),
                *self._mounts(step.place, scratch=scratch),
                self.image,
                _TOOLCHAIN,
                "exec",
                _SLIDEV,
                step.name,
                *step.arguments,
            ),
            step=step,
            how=_HowItIsRun(
                in_directory=None,
                environment=_a_client_environment(),
                bound=self.bound,
                given_up=lambda: self._removed(container, step),
            ),
        )

    def _bounds(self) -> tuple[str, ...]:
        """What one build may take of this machine: memory, processes, disk."""
        held = (f"--memory={self.memory}", f"--pids-limit={_PROCESSES_AT_MOST}")
        if self.disk is None:
            return held
        return (*held, f"--storage-opt=size={self.disk}")

    def _mounts(self, place: BuildPlace, *, scratch: Path) -> tuple[str, ...]:
        """The deck read-only in, its toolchain's scratch, and the run's own out."""
        return (
            self._mount(place, _DECK_IN, "readonly"),
            f"--tmpfs={scratch}:{_SCRATCH_IS_EVERYONES}",
            self._mount(place, _OUT),
        )

    def _mount(self, place: BuildPlace, directory: str, *access: str) -> str:
        return "--mount=" + ",".join(
            (
                "type=volume",
                f"src={self.volume}",
                f"dst={place.here / directory}",
                f"volume-subpath={place.run / directory}",
                *access,
            ),
        )

    def _removed(self, container: str, step: BuildStep) -> None:
        """Take down what a step that was given up on left running.

        Killing the client that started a container leaves the container
        itself, and a deck's own code is exactly what does not stop when asked.
        A daemon that will not answer about it is named in the log rather than
        waited on, because every later build stands behind this one.
        """
        try:
            removal = subprocess.run(
                [_DOCKER, "rm", "--force", container],
                check=False,
                capture_output=True,
                env=_a_client_environment(),
                # Never longer than the step it belongs to was allowed to
                # take: every later build waits behind this one call.
                timeout=min(self.bound, _REMOVAL_WITHIN).total_seconds(),
            )
        except (OSError, subprocess.TimeoutExpired) as stayed:
            _log.error(_CONTAINER_STAYED, step.name, step.slug, stayed)
            return
        if removal.returncode != 0:
            _log.error(_CONTAINER_STAYED, step.name, step.slug, _tail(removal.stderr))


@dataclass(frozen=True, slots=True, kw_only=True)
class HostToolchain:
    """Runs Slidev out of a Node project on this machine, as this process.

    A deck's own components then run with this server's rights over the
    filesystem and the network; only the environment, the time, and where the
    result may stand are bounded. It is what a development run builds with, and
    only a deployment that says so in as many words gets it.
    """

    project: Path
    bound: timedelta

    def ran(self, step: BuildStep) -> BuildFailure | None:
        """Run that step out of this machine's own toolchain project."""
        return _what_a_step_said(
            (_TOOLCHAIN, "exec", _SLIDEV, step.name, *step.arguments),
            step=step,
            how=_HowItIsRun(
                in_directory=self.project,
                environment=_the_environment_of_this_server(),
                bound=self.bound,
                given_up=_nothing_outlives_the_tree,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SlidevBuilds:
    """Builds decks with the Slidev toolchain into directories under one root."""

    builds: Path
    toolchain: DeckToolchain
    mirrors: SourceMirrors
    output_megabytes: int

    def build(self, deck: Deck, *, source: Source) -> Artefacts | BuildFailure:
        """Build this deck at its commit, or say what came of it instead.

        The talk and the PDF are written under a directory of this build's own
        that nothing points at yet, and the directory the deck delivered from
        before is left where it stands. What the toolchain said about a failure
        travels back with it, because the deck's page shows it (line 9).
        """
        place = self._a_place_of_its_own()
        broke = self._what_broke(deck, source=source, place=place)
        if broke is None:
            written = place.here / _OUT
            return Artefacts(directory=written / _TALK, pdf=written / _PDF_FILE)
        self._taken_away(place)
        return broke

    def holds(self, artefacts: Artefacts) -> bool:
        """Whether both artefacts really stand under the root builds are kept in."""
        root = self.builds.resolve()
        return all(
            written.exists() and written.resolve().is_relative_to(root)
            for written in (artefacts.directory, artefacts.pdf)
        )

    def _a_place_of_its_own(self) -> BuildPlace:
        """Where this run works: a directory of its own under the root.

        Its name is this server's own and carries nothing of the deck, because
        it becomes a container's name and a mount's subpath — and a folder name
        somebody else chose would be somebody else's punctuation in a command
        line of ours.
        """
        root = self.builds.absolute()
        root.mkdir(parents=True, exist_ok=True)
        run = Path(mkdtemp(dir=root))
        (run / _OUT).mkdir()
        return BuildPlace(root=root, run=Path(run.name))

    def _what_broke(
        self,
        deck: Deck,
        *,
        source: Source,
        place: BuildPlace,
    ) -> BuildFailure | None:
        """Write the deck out, build the talk, export the PDF, or say what broke."""
        if not self._exported(deck, source=source, into=place.here / _DECK_IN):
            # A tree this server cannot read out of its own mirror is a fault
            # of this host, not words a deck's author could act on.
            return BuildFailure(text=None)
        entry = str(place.here / _DECK_IN / SLIDES_FILE)
        written = place.here / _OUT
        steps = (
            (
                _BUILD,
                (
                    entry,
                    "--base",
                    talk_address(deck.slug),
                    "--out",
                    str(written / _TALK),
                ),
            ),
            (_EXPORT, (entry, "--output", str(written / _PDF_FILE))),
        )
        # The walk stops at the first step that failed, so a deck that does not
        # build is never exported either.
        for name, arguments in steps:
            broke = self.toolchain.ran(
                BuildStep(place=place, slug=deck.slug, name=name, arguments=arguments),
            )
            if broke is not None:
                return broke
        return self._more_than_it_may_leave(deck, place)

    def _more_than_it_may_leave(
        self,
        deck: Deck,
        place: BuildPlace,
    ) -> BuildFailure | None:
        """Refuse a build whose talk is larger than one build may leave here.

        A deck's own code decides what its talk holds, and a talk this machine
        cannot keep is a machine that stops keeping the others. No toolchain
        failed, so the words the page shows are this server's own.
        """
        written = _megabytes_under(place.here / _OUT)
        if written <= self.output_megabytes:
            return None
        _log.warning(_TOO_MUCH_WRITTEN, deck.slug, written)
        return BuildFailure(
            text=_TOO_MUCH_FOR_A_PAGE.format(
                written=written,
                allowed=self.output_megabytes,
            ),
        )

    def _taken_away(self, place: BuildPlace) -> None:
        """Take away what a build that did not finish left, however it left it.

        Nothing points at it and nothing ever will, and the directory the deck
        delivers from is not touched; cleaning up the ones that were pointed at
        is line 20. A deck's own code wrote in this tree and may have closed a
        directory behind it, so the modes are given back first. Whether the
        tree really went is then read off the filesystem, and one that stayed
        is named in the log rather than passed over, because it is this machine
        filling up.
        """
        _opened_again(place.here)
        shutil.rmtree(place.here, ignore_errors=True)
        if place.here.exists():
            _log.warning(_RUN_STAYED, place.run)

    def _exported(self, deck: Deck, *, source: Source, into: Path) -> bool:
        """Write the deck's tree at its commit out of the mirror, as files."""
        try:
            self.mirrors.of(source).export(
                Revision(ref=source.ref, commit=deck.commit),
                deck.slug,
                into=into,
            )
        except MirrorError as unreadable:
            _log.warning(_UNREADABLE_DECK, deck.slug, deck.commit, unreadable)
            return False
        return True


def the_daemon_of_this_machine() -> Daemon:
    """What the container daemon here is, refusing one no build may run on.

    A daemon that cannot be asked, and one too old to keep a build inside its
    own directory of a volume, are both refusals: an instance that builds decks
    in containers does not start against either.
    """
    spoken = _what_the_client_said("version", "{{.Server.APIVersion}}")
    if _as_numbers(spoken) < _MOUNT_SUBPATH_SINCE:
        raise DaemonRefusedError(_TOO_OLD.format(spoken=spoken))
    driver, _, backing = _what_the_client_said("info", _THE_DRIVER).partition(" ")
    return Daemon(
        api_version=spoken,
        storage_driver=driver,
        backing_filesystem=backing,
    )


def _what_the_client_said(about: str, template: str) -> str:
    """Ask the client one question about the daemon, or refuse this machine."""
    try:
        asked = subprocess.run(
            [_DOCKER, about, "--format", template],
            check=False,
            capture_output=True,
            env=_the_environment_of_this_server(),
            timeout=_DAEMON_ANSWERS_WITHIN.total_seconds(),
        )
    except (OSError, subprocess.TimeoutExpired) as unavailable:
        raise DaemonRefusedError(_NO_CLIENT.format(said=unavailable)) from None
    if asked.returncode != 0:
        raise DaemonRefusedError(_NO_DAEMON.format(said=_tail(asked.stderr)))
    return asked.stdout.decode(errors="replace").strip()


def _as_numbers(version: str) -> tuple[int, ...]:
    """A daemon's version as numbers, refusing anything that is not one."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        raise DaemonRefusedError(_TOO_OLD.format(spoken=version)) from None


def _what_a_step_said(
    command: tuple[str, ...],
    *,
    step: BuildStep,
    how: _HowItIsRun,
) -> BuildFailure | None:
    """Run one step's command line under the bound, and say what came of it."""
    reading, writing = os.pipe()
    with os.fdopen(reading, "rb", buffering=0) as printed:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=writing,
                cwd=how.in_directory,
                env=how.environment,
                # A step spawns processes of its own — pnpm runs slidev, and an
                # export runs a browser under that — so only a session of its
                # own lets the bound reach every one of them, not just the
                # child this call started directly.
                start_new_session=True,
            )
        except OSError as unavailable:
            # A toolchain that cannot be started is this host's fault, and what
            # the operating system says about it names paths of this host; the
            # deck's page is told that no words came back, and the log keeps
            # them.
            _log.error(_TOOLCHAIN_UNAVAILABLE, step.name, step.slug, unavailable)
            return BuildFailure(text=None)
        finally:
            # The child holds the writing end now, and only this process
            # letting go of it lets the reading end ever see an end.
            os.close(writing)
        said, in_time = _the_end_of_what_it_printed(printed, process, bound=how.bound)
    if not in_time:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()
        how.given_up()
        _log.warning(_TOOLCHAIN_TIMED_OUT, step.name, step.slug, how.bound)
        # What a step that was killed had printed is not shown: it is the words
        # of a run nobody waited for, and the page says so in its own sentence.
        return BuildFailure(text=None)
    if process.returncode != 0:
        words = _tail(said)
        _log.warning(_TOOLCHAIN_FAILED, step.name, step.slug, words)
        return BuildFailure(text=words or None)
    return None


def _the_end_of_what_it_printed(
    printed: IO[bytes],
    process: subprocess.Popen[bytes],
    *,
    bound: timedelta,
) -> tuple[bytes, bool]:
    """The last of what a step printed, and whether it ended inside its bound.

    A deck's own build decides how much it prints, and a server that held all
    of it would be a server a deck can fill; only the end a page can show is
    ever kept, however long the step goes on talking.
    """
    ends_by = monotonic() + bound.total_seconds()
    said = b""
    while True:
        left = ends_by - monotonic()
        if left <= 0 or not select.select([printed], [], [], left)[0]:
            return said, False
        printed_now = printed.read(_READ_AT_ONCE)
        if not printed_now:
            return said, _ended_within(process, ends_by)
        said = (said + printed_now)[-_TAIL_BYTES:]


def _ended_within(process: subprocess.Popen[bytes], ends_by: float) -> bool:
    """Whether the step's own process was over by then, once it stopped talking."""
    try:
        process.wait(timeout=max(ends_by - monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        return False
    return True


def _megabytes_under(directory: Path) -> int:
    """How much of this machine that directory holds, following no link out."""
    held = 0
    for inside, _, files in os.walk(directory):
        held += sum((Path(inside) / name).lstat().st_size for name in files)
    return held // _A_MEGABYTE


def _opened_again(tree: Path) -> None:
    """Give back the modes a build may have taken from its own leftovers.

    The walk is top-down, so a directory closed behind a build is opened
    before this walk asks what is inside it.
    """
    for inside, directories, _ in os.walk(tree):
        for closed in directories:
            with suppress(OSError):
                (Path(inside) / closed).chmod(_OWNER_MAY_ENTER)


def _a_container_for(step: BuildStep) -> str:
    """One name per run and step, so a step given up on can be taken down."""
    return f"{_CONTAINER_PREFIX}-{step.name}-{step.place.run.name}"


def _nothing_outlives_the_tree() -> None:
    """A step run on this host leaves nothing beyond the process tree killed."""


def _the_environment_of_this_server() -> dict[str, str]:
    """The whole environment a step's own command line is run with."""
    return {name: os.environ[name] for name in _INHERITED if name in os.environ}


def _a_client_environment() -> dict[str, str]:
    """That, and the one version this server will speak to a daemon in."""
    return {**_the_environment_of_this_server(), _PINNED_API: _AS_A_VERSION}


def _tail(output: bytes) -> str:
    return bounded_failure(output.decode(errors="replace").strip())
