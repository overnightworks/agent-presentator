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
    Deck,
    Source,
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
# Enough of the toolchain's own words to say what broke, without turning a log
# line into a deck's whole output.
_REPORTED_OUTPUT: Final = 2000

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

    def build(self, deck: Deck, *, source: Source) -> Artefacts | None:
        """Build this deck at its commit, or say nothing came of it.

        The talk and the PDF are written under a directory of this build's own
        that nothing points at yet, and the directory the deck delivered from
        before is left where it stands.
        """
        with TemporaryDirectory() as work:
            folder = Path(work) / deck.slug
            if not self._exported(deck, source=source, into=folder):
                return None
            written = self._a_place_of_its_own(deck)
            artefacts = Artefacts(directory=written / _TALK, pdf=written / _PDF_FILE)
            if self._toolchain_ran(deck, slides=folder / SLIDES_FILE, into=artefacts):
                return artefacts
        # Nothing points at what a build that did not finish left behind, and
        # nothing ever will, so it goes; the directory the deck delivers from is
        # not touched, and cleaning up the ones that were pointed at is line 20.
        shutil.rmtree(written, ignore_errors=True)
        return None

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

    def _toolchain_ran(self, deck: Deck, *, slides: Path, into: Artefacts) -> bool:
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
        # `all` stops at the first step that failed, so a deck that does not
        # build is never exported either.
        return all(self._ran(*step, deck=deck) for step in steps)

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

    def _ran(self, step: str, *arguments: str, deck: Deck) -> bool:
        """Run one step of the toolchain, and say whether it succeeded."""
        try:
            finished = subprocess.run(
                [_TOOLCHAIN, "exec", _SLIDEV, step, *arguments],
                capture_output=True,
                check=False,
                cwd=self.toolchain,
                env=_toolchain_environment(),
                timeout=self.build_timeout.total_seconds(),
            )
        except subprocess.TimeoutExpired:
            _log.warning(_TOOLCHAIN_TIMED_OUT, step, deck.slug, self.build_timeout)
            return False
        except OSError as unavailable:
            _log.error(_TOOLCHAIN_UNAVAILABLE, step, deck.slug, unavailable)
            return False
        if finished.returncode != 0:
            _log.warning(_TOOLCHAIN_FAILED, step, deck.slug, _tail(finished.stderr))
            return False
        return True


def _toolchain_environment() -> dict[str, str]:
    """The whole environment a build runs in."""
    return {name: os.environ[name] for name in _INHERITED if name in os.environ}


def _tail(output: bytes) -> str:
    return output.decode(errors="replace").strip()[-_REPORTED_OUTPUT:]
