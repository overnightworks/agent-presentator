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
from enum import StrEnum
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
# What the one container that asks about a size is called and needs: it runs
# nothing, so it needs nothing, and its name says which start asked.
_A_SIZE: Final = "a-size"
# Two starts on one machine, and one start after another, are two containers:
# the name carries which process asked and which of its calls did.
_NAME_ENOUGH: Final = 4
_A_PROBE_NEEDS: Final = "64m"
# Whether a container's own filesystem can be held to a size is not read off
# the driver's name: overlay2 takes one over xfs with project quotas and
# refuses it everywhere else, and a wrong guess either leaves a build unbounded
# or fails every build. This machine is asked by running one container that
# does nothing, and the driver's name is only what the log tells the operator.
_THE_DRIVER: Final = "{{.Driver}}"
_NOTHING_AT_ALL: Final = "true"
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
# A descriptor pinned to a directory nobody may read is still that directory;
# this is the name the running kernel gives it, and the one way its mode can be
# changed without naming a path a build could have exchanged underneath.
_THE_DESCRIPTOR_ITSELF: Final = "/proc/self/fd/{descriptor}"
_HERE: Final = "."
# Adding up what a build wrote is walking a tree that build filled, so it is
# bounded like everything else it touches: a talk is hundreds of files, and
# adding up even a hundred thousand of them is a moment's work. Past either
# bound the build is refused rather than added up in the dark.
_ENTRIES_AT_MOST: Final = 100_000
_ADDING_UP_WITHIN: Final = timedelta(seconds=10)
# How often a step is asked what it has written by now, so a build that fills
# this machine is stopped while it runs rather than found out afterwards.
_ACCOUNTED_EVERY: Final = timedelta(seconds=2)
_UNREADABLE_DECK: Final = "deck %s cannot be read at %s: %s"
_TOOLCHAIN_FAILED: Final = "%s of deck %s failed: %s"
_TOOLCHAIN_UNAVAILABLE: Final = "%s of deck %s could not be run: %s"
_TOOLCHAIN_TIMED_OUT: Final = "%s of deck %s ran past %s and was given up"
_CONTAINER_STAYED: Final = "the container of %s of deck %s could not be removed: %s"
_RUN_STAYED: Final = "what build %s left could not be taken away"
_STILL_RUNNING: Final = "whether a container of build %s still runs is unknown: %s"
_LEFT_AS_IT_LIES: Final = (
    "a container of build %s may still be running, so what it left is taken"
    " away as it lies rather than opened first"
)
_PROBE_STAYED: Final = "the container %s that asked about a size stayed: %s"
_TOO_MUCH_WRITTEN: Final = "deck %s wrote %s MB, which is more than a build may leave"
_UNACCOUNTABLE: Final = "what the build of deck %s wrote could not be added up: %s"
_TOOK_TOO_LONG_TO_ADD_UP: Final = (
    "what the build of deck %s wrote could not be added up inside %s"
)
# What a person reads on the deck page when no toolchain failed but the build
# is refused all the same.
_TOO_MUCH_FOR_A_PAGE: Final = (
    "The talk this build wrote is {written} MB; a build may leave at most {allowed} MB."
)
_TOO_MANY_FOR_A_PAGE: Final = (
    "The talk this build wrote holds more than {allowed} files;"
    " a build may leave fewer."
)
_NO_SIZE_IS_TAKEN: Final = (
    "this machine does not run a container under a size for its own filesystem: %s"
)
_NO_CLIENT: Final = "no container client answers on this machine: {said}"
_NO_DAEMON: Final = "this machine's container daemon did not answer: {said}"
_TOO_OLD: Final = (
    "this machine's container daemon speaks {spoken},"
    f" and a build needs {_AS_A_VERSION} to be given one directory of a volume"
)

_log = logging.getLogger(__name__)


class _HowItAnswered(StrEnum):
    """What one call to the container client came back with.

    A refusal is the daemon saying in as many words that it did not do the
    thing; never having said is a client that timed out or could not be run,
    and what the daemon did then is not known here.
    """

    DID = "did"
    REFUSED = "refused"
    NEVER_SAID = "never said"


class DaemonRefusedError(RuntimeError):
    """This machine's container daemon is not one a deck may be built on."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Daemon:
    """What this machine's container daemon is, in its own client's words.

    Whether it holds a container's own filesystem to a size is measured, not
    read off a name: this machine was asked to run one bounded container that
    does nothing, and it either did or said why not.
    """

    api_version: str
    storage_driver: str
    bounds_a_container_filesystem: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class _Added:
    """What adding up one run's talk found, and what stopped the adding up.

    A pass that was stopped carries no size: it says how far it came, and the
    caller refuses the build rather than believing a number nobody counted.
    """

    megabytes: int
    entries: int
    unreadable: str | None
    in_time: bool


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
class _HowItWent:
    """What a step printed, and what stopped it before it was done."""

    said: bytes
    refused: BuildFailure | None
    in_time: bool


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
    # Asked while the step runs: a build that has already written more than it
    # may leave is stopped there, because by the time it ends this machine is
    # full. It answers with what the deck's page will say, or with nothing.
    watched: Callable[[], BuildFailure | None]


class DeckToolchain(Protocol):
    """Runs one Slidev command over the directories a build works in."""

    @abstractmethod
    def ran(self, step: BuildStep) -> BuildFailure | None:
        """Run that step, saying nothing when it did what it was asked to.

        What the toolchain printed travels back with a failure, because the
        deck's page shows it (line 9); a step that was given up on, and one
        that could not be started at all, hand back no words.
        """

    @abstractmethod
    def nothing_of_it_runs(self, place: BuildPlace) -> bool:
        """Whether nothing this build started can still be writing in there.

        What a build left is only safe to take in hand once nothing of that
        build is still there to change it under this server's hands. An
        answer nobody could get is a no.
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

    def nothing_of_it_runs(self, place: BuildPlace) -> bool:
        """Whether the daemon still has a container of this run's on its feet."""
        try:
            asked = subprocess.run(
                [_DOCKER, "ps", "--quiet", "--filter", f"name={place.run.name}"],
                check=False,
                capture_output=True,
                env=_a_client_environment(),
                timeout=min(self.bound, _REMOVAL_WITHIN).total_seconds(),
            )
        except (OSError, subprocess.TimeoutExpired) as unanswered:
            _log.warning(_STILL_RUNNING, place.run, unanswered)
            return False
        if asked.returncode != 0:
            _log.warning(_STILL_RUNNING, place.run, _tail(asked.stderr))
            return False
        return not asked.stdout.strip()

    def _removed(self, container: str, step: BuildStep) -> None:
        """Take down what a step that was given up on left running.

        Killing the client that started a container leaves the container
        itself, and a deck's own code is exactly what does not stop when asked.
        A daemon that will not answer about it is named in the log rather than
        waited on, because every later build stands behind this one.
        """
        # Never longer than the step it belongs to was allowed to take: every
        # later build waits behind this one call.
        stayed = _taken_down(container, within=min(self.bound, _REMOVAL_WITHIN))
        if stayed is not None:
            _log.error(_CONTAINER_STAYED, step.name, step.slug, stayed)


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

    def nothing_of_it_runs(self, place: BuildPlace) -> bool:
        """Nothing does: a step of this build was waited for where it was killed."""
        del place
        return True

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

    def _taken_away(self, place: BuildPlace) -> None:
        """Take away what a build that did not finish left, however it left it.

        Nothing points at it and nothing ever will, and the directory the deck
        delivers from is not touched; cleaning up the ones that were pointed at
        is line 20. A deck's own code wrote in this tree and may have closed a
        directory behind it, so the modes are given back first — but only once
        nothing of that build is running any more, because opening a directory
        for a build that is still writing in it is opening it for the build.
        Whether the tree really went is then read off the filesystem, and one
        that stayed is named in the log rather than passed over, because it is
        this machine filling up.
        """
        if self.toolchain.nothing_of_it_runs(place):
            _opened_again(place.here)
        else:
            _log.warning(_LEFT_AS_IT_LIES, place.run)
        shutil.rmtree(place.here, ignore_errors=True)
        if place.here.exists():
            _log.warning(_RUN_STAYED, place.run)

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
                BuildStep(
                    place=place,
                    slug=deck.slug,
                    name=name,
                    arguments=arguments,
                    watched=lambda: self._more_than_it_may_leave(deck, place),
                ),
            )
            if broke is not None:
                return broke
        return self._more_than_it_may_leave(deck, place)

    def _more_than_it_may_leave(
        self,
        deck: Deck,
        place: BuildPlace,
    ) -> BuildFailure | None:
        """Refuse a build whose talk is more than one build may leave here.

        A deck's own code decides what its talk holds, and a talk this machine
        cannot keep is a machine that stops keeping the others. It is asked
        while the build runs and once more when it is over, because a build
        that fills this machine has filled it long before it ends. No toolchain
        failed, so the words a page shows here are this server's own; what only
        this host can act on stays in the log.
        """
        added = _added_up(
            place.here / _OUT,
            ends_by=monotonic() + _ADDING_UP_WITHIN.total_seconds(),
        )
        if added.unreadable is not None:
            _log.warning(_UNACCOUNTABLE, deck.slug, added.unreadable)
            return BuildFailure(text=None)
        if not added.in_time:
            _log.warning(_TOOK_TOO_LONG_TO_ADD_UP, deck.slug, _ADDING_UP_WITHIN)
            return BuildFailure(text=None)
        if added.entries > _ENTRIES_AT_MOST:
            _log.warning(_TOO_MUCH_WRITTEN, deck.slug, added.entries)
            return BuildFailure(
                text=_TOO_MANY_FOR_A_PAGE.format(allowed=_ENTRIES_AT_MOST),
            )
        if added.megabytes <= self.output_megabytes:
            return None
        _log.warning(_TOO_MUCH_WRITTEN, deck.slug, added.megabytes)
        return BuildFailure(
            text=_TOO_MUCH_FOR_A_PAGE.format(
                written=added.megabytes,
                allowed=self.output_megabytes,
            ),
        )

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


def the_daemon_of_this_machine(*, image: str, disk: str | None) -> Daemon:
    """What the container daemon here is, refusing one no build may run on.

    A daemon that cannot be asked, one too old to keep a build inside its own
    directory of a volume, and one that does not carry the image every build
    runs in are all refusals: an instance that cannot build a deck says so at
    its start rather than once a deck is pushed. Whether it takes a size for a
    container's own filesystem is asked here too, by running one.
    """
    spoken = _what_the_client_said("version", "--format", "{{.Server.APIVersion}}")
    if _as_numbers(spoken) < _MOUNT_SUBPATH_SINCE:
        raise DaemonRefusedError(_TOO_OLD.format(spoken=spoken))
    driver = _what_the_client_said("info", "--format", _THE_DRIVER)
    _what_the_client_said("image", "inspect", "--format", "{{.Id}}", image)
    return Daemon(
        api_version=spoken,
        storage_driver=driver,
        bounds_a_container_filesystem=disk is not None
        and _a_size_is_taken(image, disk),
    )


def _a_size_is_taken(image: str, disk: str) -> bool:
    """Whether this machine really bounds a container's own filesystem.

    One container of this instance's own, bounded like a build's and doing
    nothing at all: a machine that cannot hold it says so here, once, instead
    of failing every build of every deck. The making of it is inside the same
    hands as the taking down, because a daemon that made the container and a
    client that never heard so is exactly the moment one would be left behind;
    only a daemon that answered in as many words that it made nothing leaves
    nothing to take down. The name is this start's and this call's alone.
    """
    probe = (
        f"{_CONTAINER_PREFIX}-{_A_SIZE}-{os.getpid()}-{os.urandom(_NAME_ENOUGH).hex()}"
    )
    made = _HowItAnswered.NEVER_SAID
    try:
        made = _the_client_did(
            "create",
            f"--name={probe}",
            *_SEALED,
            f"--entrypoint={_NOTHING_AT_ALL}",
            f"--memory={_A_PROBE_NEEDS}",
            f"--pids-limit={_PROCESSES_AT_MOST}",
            f"--storage-opt=size={disk}",
            image,
        )
        if made is not _HowItAnswered.DID:
            return False
        return _the_client_did("start", "--attach", probe) is _HowItAnswered.DID
    finally:
        if made is not _HowItAnswered.REFUSED:
            stayed = _taken_down(probe, within=_REMOVAL_WITHIN)
            if stayed is not None:
                _log.error(_PROBE_STAYED, probe, stayed)


def _the_client_did(*arguments: str) -> _HowItAnswered:
    """How that one call to the client came back, or that it never did."""
    try:
        asked = subprocess.run(
            [_DOCKER, *arguments],
            check=False,
            capture_output=True,
            env=_a_client_environment(),
            timeout=_DAEMON_ANSWERS_WITHIN.total_seconds(),
        )
    except (OSError, subprocess.TimeoutExpired) as unavailable:
        _log.warning(_NO_SIZE_IS_TAKEN, unavailable)
        return _HowItAnswered.NEVER_SAID
    if asked.returncode != 0:
        _log.warning(_NO_SIZE_IS_TAKEN, _tail(asked.stderr))
        return _HowItAnswered.REFUSED
    return _HowItAnswered.DID


def _taken_down(container: str, *, within: timedelta) -> str | None:
    """Take that container down, and say what stood in the way if it stayed."""
    try:
        removal = subprocess.run(
            [_DOCKER, "rm", "--force", container],
            check=False,
            capture_output=True,
            env=_a_client_environment(),
            timeout=within.total_seconds(),
        )
    except (OSError, subprocess.TimeoutExpired) as stayed:
        return str(stayed)
    if removal.returncode != 0:
        return _tail(removal.stderr)
    return None


def _what_the_client_said(*arguments: str) -> str:
    """Ask the client one question about this machine, or refuse it."""
    try:
        asked = subprocess.run(
            [_DOCKER, *arguments],
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
        went = _the_end_of_what_it_printed(printed, process, step=step, bound=how.bound)
    if went.refused is not None or not went.in_time:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()
        how.given_up()
        if went.refused is not None:
            # What it wrote is the reason, and the watcher has said it.
            return went.refused
        _log.warning(_TOOLCHAIN_TIMED_OUT, step.name, step.slug, how.bound)
        # What a step that was killed had printed is not shown: it is the words
        # of a run nobody waited for, and the page says so in its own sentence.
        return BuildFailure(text=None)
    if process.returncode != 0:
        words = _tail(went.said)
        _log.warning(_TOOLCHAIN_FAILED, step.name, step.slug, words)
        return BuildFailure(text=words or None)
    return None


def _the_end_of_what_it_printed(
    printed: IO[bytes],
    process: subprocess.Popen[bytes],
    *,
    step: BuildStep,
    bound: timedelta,
) -> _HowItWent:
    """The last of what a step printed, and what stopped it before it was done.

    A deck's own build decides how much it prints, and a server that held all
    of it would be a server a deck can fill; only the end a page can show is
    ever kept, however long the step goes on talking. The same wait is what
    asks, at every turn, whether the build has by now written more than it may
    leave — a machine is filled while a build runs, not when it ends.
    """
    ends_by = monotonic() + bound.total_seconds()
    accounted_at = monotonic()
    said = b""
    while True:
        now = monotonic()
        if now >= ends_by:
            return _HowItWent(said=said, refused=None, in_time=False)
        if now >= accounted_at:
            refused = step.watched()
            if refused is not None:
                return _HowItWent(said=said, refused=refused, in_time=True)
            # On its own turn, whatever the step is printing: a build that
            # talks without stopping would otherwise be counted at every word.
            accounted_at = monotonic() + _ACCOUNTED_EVERY.total_seconds()
        waited_for = max(min(ends_by, accounted_at) - monotonic(), 0.0)
        if not select.select([printed], [], [], waited_for)[0]:
            continue
        printed_now = printed.read(_READ_AT_ONCE)
        if not printed_now:
            return _HowItWent(
                said=said,
                refused=None,
                in_time=_ended_within(process, ends_by),
            )
        said = (said + printed_now)[-_TAIL_BYTES:]


def _ended_within(process: subprocess.Popen[bytes], ends_by: float) -> bool:
    """Whether the step's own process was over by then, once it stopped talking."""
    try:
        process.wait(timeout=max(ends_by - monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        return False
    return True


def _added_up(directory: Path, *, ends_by: float) -> _Added:
    """Add up what one run has written into that directory, so far.

    Every name in it was written by a deck's own build, so nothing here is
    reached by its path: the root is opened once, and every directory inside
    it is opened from the descriptor of the one that holds it, as a directory
    and never through a link. A name that is not a real directory by the time
    it is opened is refused rather than followed — between one look and the
    next a build can make a name into anything, and a descriptor is the one
    thing it cannot exchange. A link is counted as the name it is, the walk
    stops at the first entry it cannot read, and it stops at the entry and at
    the moment it may not go past: a talk this server could not add up is one
    it refuses, never one it guesses at.
    """
    root = _a_directory_of_its_own(directory)
    if root is None:
        return _Added(megabytes=0, entries=0, unreadable=str(directory), in_time=True)
    held, entries, still_open = 0, 0, [root]
    try:
        while still_open:
            holder = still_open.pop()
            try:
                for entry in os.scandir(holder):
                    entries += 1
                    if entries > _ENTRIES_AT_MOST:
                        return _Added(
                            megabytes=0,
                            entries=entries,
                            unreadable=None,
                            in_time=True,
                        )
                    if monotonic() > ends_by:
                        return _Added(
                            megabytes=0,
                            entries=entries,
                            unreadable=None,
                            in_time=False,
                        )
                    if entry.is_dir(follow_symlinks=False):
                        still_open.append(_opened_within(entry.name, held_by=holder))
                    elif not entry.is_symlink():
                        held += entry.stat(follow_symlinks=False).st_size
            except OSError as unreadable:
                return _Added(
                    megabytes=0,
                    entries=entries,
                    unreadable=str(unreadable),
                    in_time=True,
                )
            finally:
                os.close(holder)
    finally:
        for holder in still_open:
            os.close(holder)
    return _Added(
        megabytes=held // _A_MEGABYTE,
        entries=entries,
        unreadable=None,
        in_time=True,
    )


def _opened_within(name: str, *, held_by: int) -> int:
    """That name as a directory of its own, opened from the one holding it.

    The open is the whole check: it refuses a link and it refuses anything
    that is not a directory, at the moment it happens, so nothing a build does
    in between can turn the name it read into a way out of this run.
    """
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=held_by,
    )


def _a_directory_of_its_own(directory: Path) -> int | None:
    """That directory as a descriptor, or nothing where it is not one."""
    try:
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None


def _opened_again(tree: Path) -> None:
    """Give back the modes a build may have taken from its own leftovers.

    Every directory is pinned as a descriptor before anything is done to it,
    and the mode is changed through that descriptor rather than through the
    name it had, which a build may since have made into a link to anything of
    this machine's. A directory a build closed cannot be opened for reading at
    all, so it is pinned without reading it and opened again through itself.
    """
    pinned = _pinned(tree)
    while pinned:
        holder = pinned.pop()
        try:
            _let_the_owner_back_into(holder, and_pin=pinned)
        finally:
            os.close(holder)


def _let_the_owner_back_into(holder: int, *, and_pin: list[int]) -> None:
    """Open that one directory to its owner again, and pin the ones in it."""
    with suppress(OSError):
        # A descriptor pinned without reading rights cannot be changed
        # directly; this is where the running kernel lets its owner name it.
        Path(_THE_DESCRIPTOR_ITSELF.format(descriptor=holder)).chmod(_OWNER_MAY_ENTER)
        readable = os.open(_HERE, os.O_RDONLY | os.O_DIRECTORY, dir_fd=holder)
        try:
            _pin_the_directories_in(readable, and_pin=and_pin)
        finally:
            os.close(readable)


def _pin_the_directories_in(readable: int, *, and_pin: list[int]) -> None:
    """Pin every directory of that one, and no name that is not one.

    Each is pinned as it is found, so a listing that breaks off halfway still
    leaves the caller holding what it has to let go of again.
    """
    with suppress(OSError):
        for entry in os.scandir(readable):
            if entry.is_dir(follow_symlinks=False):
                with suppress(OSError):
                    and_pin.append(
                        os.open(
                            entry.name,
                            os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=readable,
                        ),
                    )


def _pinned(tree: Path) -> list[int]:
    """That tree's own directory as a descriptor, where it still is one."""
    try:
        return [os.open(tree, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)]
    except OSError:
        return []


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
