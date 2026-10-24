"""The probe driver refuses a stalled client instead of waiting for it."""

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SILENT_AFTER_PARTIAL_LINE = (
    "import sys, time; sys.stdout.write('upl'); sys.stdout.flush(); time.sleep(60)"
)
TWO_LINES = "print('uploaded'); print('closed 1.5', flush=True)"


@pytest.fixture
def lives(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    return importlib.import_module("probe_build_worker_lives")


def start(code: str) -> "subprocess.Popen[str]":
    return subprocess.Popen(  # noqa: S603
        (sys.executable, "-c", code), stdout=subprocess.PIPE, text=True
    )


def test_a_partial_handshake_line_that_stalls_is_refused_as_not_driven(
    lives: ModuleType,
) -> None:
    """defect:#455 - a stalled partial line must refuse, not hang."""
    client = start(SILENT_AFTER_PARTIAL_LINE)
    assert client.stdout is not None

    with pytest.raises(SystemExit):
        lives.handshake_line(client, client.stdout, deadline_seconds=1)

    client.wait(timeout=10)


def test_consecutive_handshake_lines_are_each_returned(lives: ModuleType) -> None:
    """defect:#455 - unbuffered reads lose no later line."""
    client = start(TWO_LINES)
    assert client.stdout is not None

    lines = [
        lives.handshake_line(client, client.stdout),
        lives.handshake_line(client, client.stdout),
    ]
    client.wait(timeout=10)

    assert lines == ["uploaded", "closed 1.5"]


def test_finished_web_client_preserves_its_run_and_refusal_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    web = importlib.import_module("probe_build_worker_web")
    program = """
print('{"run": "refused-build"}')
print('{"steps": [{"step": "build", "failed": true, "text": "Build refused"}], '
      '"staged": [], "out": [], "talk_page": false, "pdf": false}')
"""
    with subprocess.Popen(  # noqa: S603
        (sys.executable, "-c", program),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as client:
        try:
            client.wait(timeout=10)
            assert web.run_name_of(client) == "refused-build"
            assert web.outcome_of(client) == web.Outcome(
                steps=(
                    web.StepOutcome(step="build", failed=True, text="Build refused"),
                ),
                staged=(),
                out=(),
                talk_page=False,
                pdf=False,
            )
        finally:
            if client.poll() is None:
                client.kill()
            client.wait(timeout=10)
