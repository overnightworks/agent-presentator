"""Prove that the built image still builds and exports a deck, as its users.

Usage: prove_container_build.py TOOLCHAIN_IMAGE INSTANCE_IMAGE

The shipped `SlidevBuilds` with `ContainerToolchain` builds `examples/hello-deck`
in the toolchain image, and the trees of both images are usable by the users
that need them.
"""

import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from gitmirror.model import CredentialReference
from presentator.adapters.builds import ContainerToolchain, SlidevBuilds
from presentator.adapters.decks import FilesystemLocalMount, SourceMirrors
from presentator.contracts.decks import Artefacts, Deck, Source

DECK = Path(__file__).parents[1] / "examples" / "hello-deck"
BUILD_USER, WIRE_USER = "10001", "10002"
VOLUME = "proof-builds"
TOOLCHAIN_TREES = "/opt/ms-playwright /opt/corepack /app/frontend"
SERVER_TREES = "/opt/venv /opt/python"
# Install records are 0600 by design; links are judged by what they point at.
NOT_USABLE = (
    r"\( -name deb.deps -o -name rpm.deps \) -prune -o ! -type l "
    r"\( ! -readable -o \( -type d ! -executable \) "
    r"-o \( -type f -perm /111 ! -executable \) \) -print"
)
VITE_LINK = (
    'test "$(readlink "$(realpath /app/frontend/node_modules/@slidev/cli)'
    '/node_modules/.vite")" = /work/vite'
)
CHROMIUM_STARTS = (
    "/opt/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell "
    "--no-sandbox --user-data-dir=/tmp/profile --dump-dom about:blank"
)


class UnusedCredential:
    """Credentials for a file-kind source, which never uses the one resolved."""

    def resolve(self, reference: CredentialReference) -> str | None:
        """A placeholder the mirror requires and a file address ignores."""
        del reference
        return "unused-on-a-file-url"


def run(*command: str, inside: Path | None = None) -> str:
    """Run a command, failing loudly, and return what it printed."""
    return subprocess.run(
        command, cwd=inside, check=True, capture_output=True, text=True
    ).stdout.strip()


def in_image(image: str, user: str | None, script: str) -> str:
    """Run a shell script in a throwaway container of the image."""
    who = ("--user", user) if user else ()
    return run(
        "docker", "run", "--rm", "--network=none", *who, image, "sh", "-ec", script
    )


def refuse(claim: str) -> NoReturn:
    """Stop with the claim that did not hold."""
    sys.stderr.write(f"cell 2 failed: {claim}\n")
    sys.exit(1)


def commit_example_deck(repository: Path) -> str:
    """Make a git repository holding the example deck, and return its commit."""
    shutil.copytree(DECK, repository / DECK.name)
    git = ("git", "-c", "user.name=proof", "-c", "user.email=proof@example.invalid")
    run(*git, "init", "--quiet", "--initial-branch", "main", inside=repository)
    run(*git, "add", "--all", inside=repository)
    run(*git, "commit", "--quiet", "--message", "deck", inside=repository)
    return run(*git, "rev-parse", "HEAD", inside=repository)


def prove_build_and_export(image: str, scratch: Path) -> None:
    """Build and export the example deck through the shipped adapters."""
    repository, builds = scratch / "repository", scratch / "builds"
    repository.mkdir()
    builds.mkdir()
    commit = commit_example_deck(repository)
    run(
        "docker",
        "volume",
        "create",
        "--opt",
        "type=none",
        "--opt",
        "o=bind",
        "--opt",
        f"device={builds}",
        VOLUME,
    )
    source = Source(
        id="proof",
        name="proof",
        url=repository.as_uri(),
        ref="main",
        owner_id="proof",
    )
    deck = Deck(
        slug=DECK.name,
        title="proof",
        changed_at=datetime.now(UTC),
        owner_id="proof",
        source_id="proof",
        commit=commit,
        build=None,
        attempt=None,
    )
    mirrors = SourceMirrors(
        directory=scratch / "mirrors",
        credentials=UnusedCredential(),
        pull_timeout=timedelta(minutes=1),
        local_mount=FilesystemLocalMount(mount=scratch),
    )
    mirror = mirrors.of(source)
    if mirror is None or mirror.connect().revision is None:
        refuse("the example deck cannot be mirrored")
    toolchain = ContainerToolchain(
        image=image, volume=VOLUME, memory="3g", disk=None, bound=timedelta(minutes=10)
    )
    builds_of_the_proof = SlidevBuilds(
        builds=builds,
        toolchain=toolchain,
        mirrors=mirrors,
        output_megabytes=64,
    )
    built = builds_of_the_proof.build(deck, source=source)
    if not isinstance(built, Artefacts):
        refuse(f"the build failed: {built}")
    if not builds_of_the_proof.holds(built):
        refuse("the talk's page and the PDF are not both delivered")


def prove_toolchain_is_usable(image: str, instance: str) -> None:
    """Read the trees as each user, follow the vite link, start Chromium."""
    for user in (None, BUILD_USER):
        if in_image(image, user, f"find {TOOLCHAIN_TREES} {NOT_USABLE}"):
            refuse(f"toolchain trees are not usable as {user or 'presentator'}")
    if in_image(instance, WIRE_USER, f"find {SERVER_TREES} {NOT_USABLE}"):
        refuse("server trees are not usable as the wire user")
    in_image(image, None, VITE_LINK)
    in_image(image, BUILD_USER, CHROMIUM_STARTS)


def main(image: str, instance: str) -> None:
    """Prove the build, always removing the volume the proof made."""
    with tempfile.TemporaryDirectory() as scratch:
        try:
            prove_build_and_export(image, Path(scratch))
        finally:
            subprocess.run(("docker", "volume", "rm", VOLUME), check=False)
    prove_toolchain_is_usable(image, instance)
    sys.stdout.write("cell 2: hello-deck built and exported; toolchain usable\n")


if __name__ == "__main__":
    main(*sys.argv[1:3])
