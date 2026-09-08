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
"""

import logging
import os
import shutil
import signal
import subprocess
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import mkdtemp
from typing import Final, Protocol

from gitmirror.model import MirrorError, Revision
from presentator.adapters.decks import SourceMirrors
from presentator.contracts.decks import (
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
_UNREADABLE_DECK: Final = "deck %s cannot be read at %s: %s"
_TOOLCHAIN_FAILED: Final = "%s of deck %s failed: %s"
_TOOLCHAIN_UNAVAILABLE: Final = "%s of deck %s could not be run: %s"
_TOOLCHAIN_TIMED_OUT: Final = "%s of deck %s ran past %s and was given up"
_CONTAINER_STAYED: Final = "the container of %s of deck %s could not be removed: %s"

_log = logging.getLogger(__name__)


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
    writes is the container's own and goes with it.
    """

    image: str
    volume: str
    memory: str
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
                f"--memory={self.memory}",
                f"--pids-limit={_PROCESSES_AT_MOST}",
                *self._mounts(step.place, scratch=scratch),
                self.image,
                _TOOLCHAIN,
                "exec",
                _SLIDEV,
                step.name,
                *step.arguments,
            ),
            step=step,
            in_directory=None,
            bound=self.bound,
            given_up=lambda: self._removed(container, step),
        )

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
        """
        removal = subprocess.run(
            [_DOCKER, "rm", "--force", container],
            check=False,
            capture_output=True,
            env=_the_environment_of_this_server(),
        )
        if removal.returncode != 0:
            _log.error(_CONTAINER_STAYED, step.name, step.slug, _tail(removal.stderr))


@dataclass(frozen=True, slots=True, kw_only=True)
class HostToolchain:
    """Runs Slidev out of a Node project on this machine, as this process.

    A deck's own components then run with this server's rights over the
    filesystem and the network; only the environment, the time, and where the
    result may stand are bounded. It is what a development run builds with;
    `ContainerToolchain` is what an instance carrying anyone's decks builds
    with.
    """

    project: Path
    bound: timedelta

    def ran(self, step: BuildStep) -> BuildFailure | None:
        """Run that step out of this machine's own toolchain project."""
        return _what_a_step_said(
            (_TOOLCHAIN, "exec", _SLIDEV, step.name, *step.arguments),
            step=step,
            in_directory=self.project,
            bound=self.bound,
            given_up=_nothing_outlives_the_tree,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SlidevBuilds:
    """Builds decks with the Slidev toolchain into directories under one root."""

    builds: Path
    toolchain: DeckToolchain
    mirrors: SourceMirrors

    def build(self, deck: Deck, *, source: Source) -> Artefacts | BuildFailure:
        """Build this deck at its commit, or say what came of it instead.

        The talk and the PDF are written under a directory of this build's own
        that nothing points at yet, and the directory the deck delivered from
        before is left where it stands. What the toolchain said about a failure
        travels back with it, because the deck's page shows it (line 9).
        """
        place = self._a_place_of_its_own(deck)
        broke = self._what_broke(deck, source=source, place=place)
        if broke is None:
            written = place.here / _OUT
            return Artefacts(directory=written / _TALK, pdf=written / _PDF_FILE)
        # Nothing points at what a build that did not finish left behind, and
        # nothing ever will, so it goes; the directory the deck delivers from is
        # not touched, and cleaning up the ones that were pointed at is line 20.
        shutil.rmtree(place.here, ignore_errors=True)
        return broke

    def holds(self, artefacts: Artefacts) -> bool:
        """Whether both artefacts really stand under the root builds are kept in."""
        root = self.builds.resolve()
        return all(
            written.exists() and written.resolve().is_relative_to(root)
            for written in (artefacts.directory, artefacts.pdf)
        )

    def _a_place_of_its_own(self, deck: Deck) -> BuildPlace:
        """Where this run works: a new directory under the root, under the slug.

        The deck's tree and everything the run writes stand side by side in it,
        because a toolchain running elsewhere is given those two directories
        and nothing else of this filesystem.
        """
        root = self.builds.absolute()
        for_the_deck = root / deck.slug
        for_the_deck.mkdir(parents=True, exist_ok=True)
        run = Path(mkdtemp(prefix=f"{deck.commit}-", dir=for_the_deck))
        (run / _OUT).mkdir()
        return BuildPlace(root=root, run=run.relative_to(root))

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
        return None

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


def _what_a_step_said(
    command: tuple[str, ...],
    *,
    step: BuildStep,
    in_directory: Path | None,
    bound: timedelta,
    given_up: Callable[[], None],
) -> BuildFailure | None:
    """Run one step's command line under the bound, and say what came of it."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=in_directory,
            env=_the_environment_of_this_server(),
            # A step spawns processes of its own — pnpm runs slidev, and an
            # export runs a browser under that — so only a session of its own
            # lets the bound reach every one of them, not just the child this
            # call started directly.
            start_new_session=True,
        )
    except OSError as unavailable:
        # A toolchain that cannot be started is this host's fault, and what the
        # operating system says about it names paths of this host; the deck's
        # page is told that no words came back, and the log keeps them.
        _log.error(_TOOLCHAIN_UNAVAILABLE, step.name, step.slug, unavailable)
        return BuildFailure(text=None)
    with process:
        try:
            _, stderr = process.communicate(timeout=bound.total_seconds())
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait()
            given_up()
            _log.warning(_TOOLCHAIN_TIMED_OUT, step.name, step.slug, bound)
            # What a step that was killed had written so far is not read: the
            # pipe is the killed tree's, and reading it would wait on the very
            # processes the bound gave up on.
            return BuildFailure(text=None)
    if process.returncode != 0:
        said = _tail(stderr)
        _log.warning(_TOOLCHAIN_FAILED, step.name, step.slug, said)
        return BuildFailure(text=said or None)
    return None


def _a_container_for(step: BuildStep) -> str:
    """One name per run and step, so a step given up on can be taken down."""
    return f"{_CONTAINER_PREFIX}-{step.name}-{step.place.run.name}"


def _nothing_outlives_the_tree() -> None:
    """A step run on this host leaves nothing beyond the process tree killed."""


def _the_environment_of_this_server() -> dict[str, str]:
    """The whole environment a step's own command line is run with."""
    return {name: os.environ[name] for name in _INHERITED if name in os.environ}


def _tail(output: bytes) -> str:
    return bounded_failure(output.decode(errors="replace").strip())
