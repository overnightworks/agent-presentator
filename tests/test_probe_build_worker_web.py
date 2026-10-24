"""The web probe's client lifecycle and its ordering proofs of the web's wait."""

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CLIENT_SAYS = (
    'import json; print(json.dumps({"steps": [], "staged": [], "out": [],'
    ' "talk_page": False, "pdf": False}))'
)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    return importlib.import_module("probe_build_worker_web")


def test_a_started_client_is_collected_after_its_program_was_sent(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    program = tmp_path / "client.py"
    program.write_text(CLIENT_SAYS, encoding="utf-8")
    monkeypatch.setattr(probe, "CLIENT_PROGRAM", program)
    monkeypatch.setattr(probe, "web_command", lambda *_: (sys.executable, "-I", "-"))

    client = probe.start_client("case", "build")

    assert probe.outcome_of(client).steps == ()


def test_a_web_that_answers_before_the_worker_started_is_refused(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "web_answers", lambda: True)

    with pytest.raises(SystemExit):
        probe.refuse_if_the_web_answers("early")


def test_a_silent_web_is_not_refused(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "web_answers", lambda: False)

    probe.refuse_if_the_web_answers("early")


BEGAN = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def life_began(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "state", lambda: ("started", "1"))
    monkeypatch.setattr(probe, "docker_time", lambda _: BEGAN)


@pytest.mark.usefixtures("life_began")
def test_an_answer_before_the_worker_life_began_is_refused(probe: ModuleType) -> None:
    with pytest.raises(SystemExit):
        probe.refuse_unless_after_the_worker_began(BEGAN - timedelta(seconds=1))


@pytest.mark.usefixtures("life_began")
def test_an_answer_after_the_worker_life_began_is_accepted(probe: ModuleType) -> None:
    probe.refuse_unless_after_the_worker_began(BEGAN)
