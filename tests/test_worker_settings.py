"""The build worker reads its own bounded settings, and nothing of the web's."""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from presentator.host.config import (
    ConfigurationError,
    Settings,
    WorkerSettings,
    load_worker_settings,
    worker_settings_variables,
)

TIMEOUT_VARIABLE = "PRESENTATOR_BUILD_TIMEOUT_SECONDS"
MEGABYTES_VARIABLE = "PRESENTATOR_BUILD_OUTPUT_MEGABYTES"
SUBPROCESS_SECONDS = 30
DESIGNED_DEFAULT_SECONDS = 300.0
DESIGNED_DEFAULT_MEGABYTES = 300
UNIQUE_BAD_VALUE = "hunter2-seconds"


@pytest.fixture(autouse=True)
def bare_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*worker_settings_variables(), "PRESENTATOR_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_the_default_needs_no_variable_of_the_web_and_is_the_web_default() -> None:
    settings = load_worker_settings()

    assert settings.build_timeout_seconds == DESIGNED_DEFAULT_SECONDS
    assert (
        settings.build_timeout_seconds
        == Settings.model_fields["build_timeout_seconds"].default
    )


def test_the_megabytes_default_is_the_web_default() -> None:
    megabytes = load_worker_settings().build_output_megabytes

    assert megabytes == DESIGNED_DEFAULT_MEGABYTES
    assert megabytes == Settings.model_fields["build_output_megabytes"].default


def test_the_environment_sets_the_megabytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MEGABYTES_VARIABLE, "45")

    assert load_worker_settings() == WorkerSettings(build_output_megabytes=45)


def test_the_environment_overrides_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, "45")

    assert load_worker_settings() == WorkerSettings(build_timeout_seconds=45.0)


def test_a_dot_env_in_the_working_directory_is_ignored(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(f"{TIMEOUT_VARIABLE}=45\n")

    assert load_worker_settings().build_timeout_seconds == DESIGNED_DEFAULT_SECONDS


REFUSED_VALUES = {
    "text": "abc",
    "empty": "",
    "not a number": "nan",
    "infinite": "inf",
    "negative infinite": "-inf",
    "zero": "0",
    "negative": "-5",
    "beyond what the watchdog holds": "1e300",
    "just beyond half of what the watchdog holds": repr(
        threading.TIMEOUT_MAX / 2 * (1 + 1e-9)
    ),
}


@pytest.mark.parametrize("value", REFUSED_VALUES.values(), ids=REFUSED_VALUES.keys())
def test_an_unusable_timeout_is_refused_by_the_variable_name_without_its_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, value)

    with pytest.raises(ConfigurationError) as refused:
        load_worker_settings()

    assert TIMEOUT_VARIABLE in str(refused.value)


@pytest.mark.parametrize("value", ["abc", "", "0", "-1", "1.5"])
def test_an_unusable_megabytes_is_refused_by_the_variable_name_without_its_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MEGABYTES_VARIABLE, value)

    with pytest.raises(ConfigurationError) as refused:
        load_worker_settings()

    assert MEGABYTES_VARIABLE in str(refused.value)
    assert TIMEOUT_VARIABLE not in str(refused.value)


def test_a_refusal_never_quotes_the_value_it_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, UNIQUE_BAD_VALUE)

    with pytest.raises(ConfigurationError) as refused:
        load_worker_settings()

    assert UNIQUE_BAD_VALUE not in str(refused.value)


def test_the_variables_of_the_worker_are_derived_from_its_fields() -> None:
    assert worker_settings_variables() == {
        TIMEOUT_VARIABLE,
        MEGABYTES_VARIABLE,
    }


def test_importing_the_worker_reads_no_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, "abc")

    imported = subprocess.run(
        [sys.executable, "-I", "-c", "import presentator.host.worker"],
        capture_output=True,
        timeout=SUBPROCESS_SECONDS,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr
