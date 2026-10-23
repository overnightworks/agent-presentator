import subprocess
import sys

import pytest

SILENT_AFTER_PARTIAL_LINE = (
    "import sys, time; sys.stdout.write('upl'); sys.stdout.flush(); time.sleep(60)"
)
TWO_LINES = "print('uploaded'); print('closed 1.5', flush=True)"


@pytest.fixture
def lives(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend("scripts")
    import probe_build_worker_lives

    return probe_build_worker_lives


def start(code: str) -> "subprocess.Popen[str]":
    return subprocess.Popen(  # noqa: S603
        (sys.executable, "-c", code), stdout=subprocess.PIPE, text=True
    )


def test_a_partial_handshake_line_that_stalls_is_refused_as_not_driven(
    lives, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lives, "HANDSHAKE_SECONDS", 1)
    client = start(SILENT_AFTER_PARTIAL_LINE)
    assert client.stdout is not None

    with pytest.raises(SystemExit):
        lives.handshake_line(client, client.stdout)

    client.wait(timeout=10)


def test_consecutive_handshake_lines_are_each_returned(lives) -> None:
    client = start(TWO_LINES)
    assert client.stdout is not None

    lines = [
        lives.handshake_line(client, client.stdout),
        lives.handshake_line(client, client.stdout),
    ]
    client.wait(timeout=10)

    assert lines == ["uploaded", "closed 1.5"]
