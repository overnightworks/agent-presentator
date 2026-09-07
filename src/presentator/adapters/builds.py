"""Turning a deck's folder into the talk it delivers, with Slidev (ADR 0005).

Slidev's own command line owns the build and the PDF export, so this adapter
spawns it rather than binding a second renderer. The deck's tree is written out
of the bare mirror beside it, because a toolchain needs files.

**A deck is code.** Until the build is sandboxed, the Vue components a deck
carries execute on this host with the rights of this process. What is bounded
here is the environment the child is given, the time it may take, and where its
result is allowed to stand; isolation itself is the sandbox's job.
"""

import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Final

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
_BUILD: Final = "build"
_EXPORT: Final = "export"
# What the talk stands under and what the PDF is called, inside the one
# directory a build writes into.
_TALK: Final = "talk"
_PDF_FILE: Final = "deck.pdf"
# Only what a Node toolchain needs to run; the child inherits nothing else, so
# neither a source's read-only secret nor this instance's key is in reach of a
# deck's own build-time code.
_INHERITED: Final = ("PATH", "HOME")
_UNREADABLE_DECK: Final = "deck %s cannot be read at %s: %s"
_TOOLCHAIN_FAILED: Final = "%s of deck %s failed: %s"
_TOOLCHAIN_UNAVAILABLE: Final = "%s of deck %s could not be run: %s"
_TOOLCHAIN_TIMED_OUT: Final = "%s of deck %s ran past %s and was given up"

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SlidevBuilds:
    """Builds decks with the Slidev toolchain into directories under one root."""

    builds: Path
    toolchain: Path
    mirrors: SourceMirrors
    build_timeout: timedelta

    def build(self, deck: Deck, *, source: Source) -> Artefacts | BuildFailure:
        """Build this deck at its commit, or say what came of it instead.

        The talk and the PDF are written under a directory of this build's own
        that nothing points at yet, and the directory the deck delivered from
        before is left where it stands. What the toolchain said about a failure
        travels back with it, because the deck's page shows it (line 9).
        """
        with TemporaryDirectory() as work:
            folder = Path(work) / deck.slug
            if not self._exported(deck, source=source, into=folder):
                # A tree this server cannot read out of its own mirror is a
                # fault of this host, not words a deck's author could act on.
                return BuildFailure(text=None)
            written = self._a_place_of_its_own(deck)
            artefacts = Artefacts(directory=written / _TALK, pdf=written / _PDF_FILE)
            broke = self._what_broke(deck, slides=folder / SLIDES_FILE, into=artefacts)
            if broke is None:
                return artefacts
        # Nothing points at what a build that did not finish left behind, and
        # nothing ever will, so it goes; the directory the deck delivers from is
        # not touched, and cleaning up the ones that were pointed at is line 20.
        shutil.rmtree(written, ignore_errors=True)
        return broke

    def holds(self, artefacts: Artefacts) -> bool:
        """Whether both artefacts really stand under the root builds are kept in."""
        root = self.builds.resolve()
        return all(
            written.exists() and written.resolve().is_relative_to(root)
            for written in (artefacts.directory, artefacts.pdf)
        )

    def _a_place_of_its_own(self, deck: Deck) -> Path:
        """Where this run writes: a new directory under the root, under the slug."""
        for_the_deck = self.builds / deck.slug
        for_the_deck.mkdir(parents=True, exist_ok=True)
        return Path(mkdtemp(prefix=f"{deck.commit}-", dir=for_the_deck))

    def _what_broke(
        self,
        deck: Deck,
        *,
        slides: Path,
        into: Artefacts,
    ) -> BuildFailure | None:
        """Build the talk and export the PDF, or say which of them failed."""
        entry = str(slides)
        steps = (
            (
                _BUILD,
                entry,
                "--base",
                talk_address(deck.slug),
                "--out",
                str(into.directory),
            ),
            (_EXPORT, entry, "--output", str(into.pdf)),
        )
        # The walk stops at the first step that failed, so a deck that does not
        # build is never exported either.
        for step in steps:
            broke = self._ran(*step, deck=deck)
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

    def _ran(self, step: str, *arguments: str, deck: Deck) -> BuildFailure | None:
        """Run one step of the toolchain, and say nothing when it succeeded."""
        try:
            process = subprocess.Popen(
                [_TOOLCHAIN, "exec", _SLIDEV, step, *arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=self.toolchain,
                env=_toolchain_environment(),
                # A deck's own toolchain tree spawns processes of its own —
                # pnpm runs slidev, and an export runs Chromium under that — so
                # only a session of its own lets the bound below reach every one
                # of them, not just the child this call started directly.
                start_new_session=True,
            )
        except OSError as unavailable:
            # A toolchain that cannot be started is this host's fault, and what
            # the operating system says about it names paths of this host; the
            # deck's page is told that no words came back, and the log keeps
            # them.
            _log.error(_TOOLCHAIN_UNAVAILABLE, step, deck.slug, unavailable)
            return BuildFailure(text=None)
        with process:
            try:
                _, stderr = process.communicate(
                    timeout=self.build_timeout.total_seconds(),
                )
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait()
                _log.warning(_TOOLCHAIN_TIMED_OUT, step, deck.slug, self.build_timeout)
                # What a step that was killed had written so far is not read:
                # the pipe is the killed tree's, and reading it would wait on
                # the very processes the bound gave up on.
                return BuildFailure(text=None)
        if process.returncode != 0:
            said = _tail(stderr)
            _log.warning(_TOOLCHAIN_FAILED, step, deck.slug, said)
            return BuildFailure(text=said or None)
        return None


def _toolchain_environment() -> dict[str, str]:
    """The whole environment a build runs in."""
    return {name: os.environ[name] for name in _INHERITED if name in os.environ}


def _tail(output: bytes) -> str:
    return bounded_failure(output.decode(errors="replace").strip())
