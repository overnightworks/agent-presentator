"""Build one deck through the web's own composition.

Usage: `python -I - CASE [STEP...]`, the program on stdin.

Run inside the `presentator` service as its own user. It reads the settings
with `load_settings()`, takes the worker from `the_build_worker` and runs the
named steps of a deck in a fresh run directory under the builds root. It prints
the run's name as the first JSON line, so a driver can watch the run directory,
and the outcome as the last one: each step's `BuildFailure` text, and what the
run directory holds when the steps are over.
"""

import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from shutil import copytree
from tempfile import mkdtemp

from presentator.adapters.build_wire import MAXIMUM_DEPTH, MEGABYTE
from presentator.adapters.builds import BuildPlace, BuildStep, slidev_arguments
from presentator.contracts.decks import (
    DECK_DIRECTORY,
    OUTPUT_DIRECTORY,
    PDF_FILE,
    TALK_DIRECTORY,
    StepKind,
)
from presentator.host.config import load_settings
from presentator.host.main import build_bound_of, the_build_worker

DECKS = Path("/decks")
HELLO_DECK = DECKS / "hello-deck"
FLOOD_DECK = DECKS / "probe" / "flood"
LIMITS_DECK = DECKS / "probe" / "limits"
LIMIT_FILE = "limit.txt"
STAGED_PREFIX = "staged-"
EXTRA_PART = "a"


class Case(StrEnum):
    """The decks this client builds, each by the way it reaches a limit."""

    HELLO = "hello"
    HOLD = "hold"
    MANY_ENTRIES = "many-entries"
    OUTPUT_MEGABYTES = "output-megabytes"
    OUTPUT_ENTRIES = "output-entries"
    OUTPUT_SHAPE = "output-shape"
    DECK_MEGABYTES = "deck-megabytes"
    DECK_PATHS = "deck-paths"


@dataclass(frozen=True, slots=True)
class Source:
    """The deck a case starts from, and the file it adds to the run's copy."""

    deck: Path
    limit: str | None = None


SOURCES = {
    Case.HELLO: Source(HELLO_DECK),
    Case.HOLD: Source(FLOOD_DECK),
    Case.MANY_ENTRIES: Source(LIMITS_DECK, Case.MANY_ENTRIES),
    Case.OUTPUT_MEGABYTES: Source(LIMITS_DECK, Case.OUTPUT_MEGABYTES),
    Case.OUTPUT_ENTRIES: Source(LIMITS_DECK, Case.OUTPUT_ENTRIES),
    Case.OUTPUT_SHAPE: Source(LIMITS_DECK, Case.OUTPUT_SHAPE),
    Case.DECK_MEGABYTES: Source(LIMITS_DECK),
    Case.DECK_PATHS: Source(LIMITS_DECK),
}


def swell(deck: Path, case: Case, megabytes: int) -> None:
    """Make the run's copy of the limits deck too large, or too deeply nested."""
    if case is Case.DECK_MEGABYTES:
        with (deck / "sparse").open("wb") as sparse:
            sparse.truncate(megabytes * MEGABYTE + 1)
    if case is Case.DECK_PATHS:
        deep = deck.joinpath(*[EXTRA_PART] * MAXIMUM_DEPTH)
        deep.mkdir(parents=True)
        (deep / EXTRA_PART).write_text("x", encoding="utf-8")


def prepared_run(case: Case, builds: Path, megabytes: int) -> BuildPlace:
    """A fresh run directory holding the case's deck and an empty out/."""
    run = Path(mkdtemp(dir=builds))
    (run / OUTPUT_DIRECTORY).mkdir()
    source = SOURCES[case]
    deck = run / DECK_DIRECTORY
    copytree(source.deck, deck)
    if source.limit is not None:
        (deck / LIMIT_FILE).write_text(source.limit, encoding="utf-8")
    swell(deck, case, megabytes)
    return BuildPlace(root=builds, run=Path(run.name))


def staged_names(place: BuildPlace) -> list[str]:
    """The staging directories a step left in the run directory."""
    return sorted(
        path.name
        for path in place.here.iterdir()
        if path.name.startswith(STAGED_PREFIX)
    )


def delivered(place: BuildPlace) -> dict[str, bool]:
    """Whether the talk's page and the PDF reached out/."""
    out = place.here / OUTPUT_DIRECTORY
    pdf = out / PDF_FILE
    return {
        "talk_page": (out / TALK_DIRECTORY / "index.html").is_file(),
        "pdf": pdf.is_file() and pdf.read_bytes().startswith(b"%PDF-"),
    }


def main(case: Case, kinds: list[StepKind]) -> None:
    """Run the steps and print the run's name, then the outcome."""
    settings = load_settings()
    toolchain = the_build_worker(settings, bound=build_bound_of(settings))
    place = prepared_run(case, settings.builds, settings.build_output_megabytes)
    sys.stdout.write(json.dumps({"run": place.run.name}) + "\n")
    sys.stdout.flush()
    steps: list[dict[str, object]] = []
    for kind in kinds:
        failure = toolchain.ran(
            BuildStep(
                place=place,
                slug=case.value,
                name=kind,
                arguments=slidev_arguments(kind, place=place, slug=case.value),
                watched=lambda: None,
            )
        )
        step: dict[str, object] = {"step": kind.value, "failed": failure is not None}
        if failure is not None:
            step["text"] = failure.text
        steps.append(step)
        if failure is not None:
            break
    sys.stdout.write(
        json.dumps(
            {
                "steps": steps,
                "staged": staged_names(place),
                "out": sorted(
                    path.name for path in (place.here / OUTPUT_DIRECTORY).iterdir()
                ),
            }
            | delivered(place)
        )
        + "\n"
    )


if __name__ == "__main__":
    main(Case(sys.argv[1]), [StepKind(name) for name in sys.argv[2:]] or list(StepKind))
