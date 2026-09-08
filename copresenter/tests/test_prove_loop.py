"""The proof script's real-mode gates do not accept the stand-in."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_PROOF = Path(__file__).resolve().parents[1] / "scripts" / "prove_loop.py"


def _load_prove_loop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prove_loop", _PROOF)
    if spec is None:
        message = f"cannot load {_PROOF}"
        raise RuntimeError(message)
    if spec.loader is None:
        message = f"no loader for {_PROOF}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prove_loop = _load_prove_loop()


def _health(speaking: object, hearing: object) -> dict[str, object]:
    return {
        "speaking": {"model": speaking, "ready": True},
        "hearing": {"model": hearing, "ready": True},
    }


def _released(**overrides: object) -> dict[str, object]:
    off = {
        "on": False,
        "speaking": False,
        "audioPlaying": False,
        "hearOpen": False,
        "micLive": False,
        "workletLive": False,
    }
    off.update(overrides)
    return off


@pytest.mark.parametrize(
    ("speaking", "hearing"),
    [
        ("stand-in", "stand-in"),
        ("stand-in", "hear-a"),
        ("voice-a", "stand-in"),
        (None, "hear-a"),
        ("voice-a", None),
    ],
)
def test_real_mode_refuses_health_that_names_the_standin(speaking, hearing) -> None:
    assert prove_loop._health_identities_are_real(_health(speaking, hearing)) is False


def test_real_mode_accepts_health_that_does_not_name_the_standin() -> None:
    assert prove_loop._health_identities_are_real(_health("voice-a", "hear-a")) is True


def test_release_assertion_rejects_a_live_capture_worklet() -> None:
    assert prove_loop._off_released(_released(workletLive=True)) is False


def test_release_assertion_requires_mic_socket_and_worklet_gone() -> None:
    assert prove_loop._off_released(_released()) is True


def test_a_failed_real_proof_names_the_assertion_that_did_not_hold() -> None:
    result = {
        "heard": "Frage",
        "answer": "Antwort",
        "audio_seconds": 1.2,
        "off_during_playback": _released(workletLive=True),
    }

    assert prove_loop._real_failure(result) == "off_during_playback"


def test_a_real_proof_has_no_failure_when_every_assertion_holds() -> None:
    result = {
        "heard": "Frage",
        "answer": "Antwort",
        "audio_seconds": 1.2,
        "off_during_playback": _released(),
    }

    assert prove_loop._real_failure(result) is None
