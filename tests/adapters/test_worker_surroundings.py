"""A gated child's working directory and streams."""

import os
import sys
from pathlib import Path

from presentator.adapters.worker_children import ChildSurroundings, spawn_gated
from tests.adapters.test_worker_children import (
    PATH,
    WAIT_SECONDS,
    end_child,
    release_and_close,
)
from tests.step_support import OWN_IDENTITY

PRINTING_ITS_DIRECTORY = "import os, sys; print(os.getcwd(), file=sys.stderr); print(1)"


def test_a_child_runs_in_the_given_directory_with_its_streams_where_it_is_told(
    tmp_path: Path,
) -> None:
    errors = tmp_path / "errors.log"
    descriptor = os.open(errors, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        child = spawn_gated(
            [sys.executable, "-I", "-c", PRINTING_ITS_DIRECTORY],
            identity=OWN_IDENTITY,
            environment={"PATH": PATH},
            pass_fds=(),
            surroundings=ChildSurroundings(
                cwd=tmp_path, discard_stdout=True, stderr=descriptor
            ),
        )
    finally:
        os.close(descriptor)
    try:
        release_and_close(child)
        waited = child.wait_within(WAIT_SECONDS)
    finally:
        end_child(child)

    assert waited == 0
    assert errors.read_text() == f"{tmp_path}\n"
