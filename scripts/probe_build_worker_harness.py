"""What the sealed build worker's probe modules share: the docker and holder calls.

The entry (`probe_build_worker.py`) and the isolation provers both drive a worker
through a holder container of the runtime user; this module holds the calls they
have in common and imports nothing but the standard library, so it resolves on
the host and in the holder, where `scripts/` is mounted at `/probe`.
"""

import io
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

REPOSITORY = Path(__file__).parents[1]
SERVICE = "build-worker"
HOLDER = "build-worker-probe-holder"
PROBE_DIRECTORY = "/probe"
PROBE_SOURCES = Path(__file__).resolve().parent / "probe_build_worker"
ENTRY_FILE = "probe_build_worker.py"
OUTPUT_DIRECTORY = Path("/out")


def refuse(claim: str) -> NoReturn:
    """Stop with the claim that did not hold."""
    sys.stderr.write(f"probe failed: {claim}\n")
    sys.exit(1)


def say(line: str) -> None:
    """Print one line and flush it, so a hung probe shows where it stopped."""
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def docker(*arguments: str) -> str:
    """Run the docker CLI, failing loudly, and return what it printed."""
    return subprocess.run(
        ("docker", *arguments), check=True, capture_output=True, text=True
    ).stdout.strip()


def compose(*arguments: str) -> str:
    """Run docker compose in the repository, failing loudly."""
    return docker("compose", "--file", str(REPOSITORY / "compose.yaml"), *arguments)


def in_holder(
    *command: str, user: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in the holder container; the caller judges the status."""
    who = ("--user", user) if user else ()
    return subprocess.run(
        ("docker", "exec", *who, HOLDER, *command),
        check=False,
        capture_output=True,
        text=True,
    )


def probe_client(*arguments: str, user: str | None = None) -> str:
    """Run a client command in the holder, failing with its error."""
    done = in_holder(
        "python",
        f"{PROBE_DIRECTORY}/{ENTRY_FILE}",
        "client",
        *arguments,
        user=user,
    )
    if done.returncode:
        refuse(f"client {' '.join(arguments)}: {done.stdout}{done.stderr}")
    return done.stdout.strip()


def worker() -> str:
    """The id of the worker container."""
    return compose("ps", "--all", "--quiet", SERVICE)


def state() -> tuple[str, str]:
    """The worker container's start time and restart count."""
    started, restarts = docker(
        "inspect", "--format", "{{.State.StartedAt}} {{.RestartCount}}", worker()
    ).split()
    return started, restarts


class Report:
    """The cells driven so far, one printed line each."""

    def __init__(self, cells: tuple[str, ...]) -> None:
        """Start with no cell driven, of the cells the run must drive."""
        self.cells = cells
        self.driven: set[str] = set()

    def cell(self, name: str, line: str) -> None:
        """Record a cell as driven and print its line."""
        self.driven.add(name)
        say(f"cell {name}: {line}")

    def finish(self) -> None:
        """Fail unless every cell was driven."""
        missing = [name for name in self.cells if name not in self.driven]
        if missing:
            refuse(f"cells not driven: {', '.join(missing)}")


def extract(source: str, destination: Path) -> None:
    """Copy a directory of the holder out through tar, since tmpfs defeats docker cp."""
    packed = subprocess.run(
        ("docker", "exec", HOLDER, "tar", "--create", "--directory", source, "."),
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
        archive.extractall(destination, filter="data")


def text_files(directory: Path) -> Iterator[str]:
    """The script, markup and page text files below a directory, decoded."""
    for path in directory.rglob("*"):
        if path.suffix in {".js", ".html"}:
            yield path.read_text(encoding="utf-8", errors="replace")
