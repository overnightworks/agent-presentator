"""What the build worker client's test modules share."""

from dataclasses import replace
from pathlib import Path

from presentator.adapters.build_wire import (
    SOCKET_NAME,
    FailedOutcome,
    RefusalLimit,
    RefusedOutcome,
)
from presentator.adapters.build_worker import STAGED_PREFIX, WorkerToolchain
from presentator.adapters.builds import BuildPlace, BuildStep
from presentator.contracts.decks import (
    DECK_DIRECTORY,
    OUTPUT_DIRECTORY,
    PDF_FILE,
    BuildFailure,
    StepKind,
)
from tests import worker_support as support
from tests.worker_support import OWN_GID, OWN_UID

MEGABYTES = 1
WINDOW = 0.6
WITHOUT_A_REASON = BuildFailure(text=None)
INDEX = b"<html>"
PDF = b"%PDF"
BUILT_TALK = support.OUTCOME_BUILT + support.talk_entries(INDEX)
BUILT_PDF = (
    support.OUTCOME_BUILT
    + support.file_entry(PDF_FILE.encode(), PDF)
    + support.end_entry(1, len(PDF))
)
BUILT_BY_STEP = {StepKind.BUILD: BUILT_TALK, StepKind.EXPORT: BUILT_PDF}


def refused_reply(limit: RefusalLimit, written: int | None = None) -> bytes:
    header = RefusedOutcome(limit=limit, written=written).model_dump_json().encode()
    return support.framed(header, b"PBW\x01\x05") + support.END


def failed_reply(text: str | None) -> bytes:
    header = FailedOutcome(text=text).model_dump_json().encode()
    return support.framed(header, b"PBW\x01\x05") + support.END


def toolchain(volume: Path, **overrides: object) -> WorkerToolchain:
    base = WorkerToolchain(
        socket_path=volume / SOCKET_NAME,
        megabytes=MEGABYTES,
        reply_bound_seconds=2.0,
        expected_uid=OWN_UID,
        expected_gid=OWN_GID,
        connect_window_seconds=WINDOW,
        first_retry_seconds=0.02,
        longest_retry_seconds=0.1,
        reply_margin_seconds=0.2,
    )
    return replace(base, **overrides)


def a_step(root: Path, kind: StepKind = StepKind.BUILD) -> BuildStep:
    place = BuildPlace(root=root / "builds", run=Path("run"))
    (place.here / DECK_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (place.here / DECK_DIRECTORY / "slides.md").write_text("# hello")
    (place.here / OUTPUT_DIRECTORY).mkdir(exist_ok=True)
    return BuildStep(
        place=place, slug="my-deck", name=kind, arguments=(), watched=lambda: None
    )


def tree_of(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def staging_of(step: BuildStep) -> Path:
    return step.place.here / f"{STAGED_PREFIX}{step.name}"
