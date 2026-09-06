"""The composition root, from the environment to a lobby that answers."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from presentator.host import main
from presentator.host.config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

_INSTANCE_KEY = "an instance key of at least thirty-two bytes"


@pytest.fixture
def bare_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRESENTATOR_SECRET_KEY", raising=False)
    monkeypatch.setenv("PRESENTATOR_DATABASE", str(tmp_path / "presentator.sqlite3"))
    return monkeypatch


@pytest.fixture
def environment(bare_environment: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    bare_environment.setenv("PRESENTATOR_SECRET_KEY", _INSTANCE_KEY)
    return bare_environment


def test_the_composition_root_serves_a_lobby_that_answers_the_login(
    environment: pytest.MonkeyPatch,
) -> None:
    served: list[FastAPI] = []

    def serve(app: "FastAPI", **_arguments: object) -> None:
        served.append(app)

    environment.setattr(main.uvicorn, "run", serve)

    main.main()

    assert len(served) == 1
    assert TestClient(served[0]).get("/login").is_success


@pytest.mark.usefixtures("bare_environment")
def test_an_instance_without_a_key_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        Settings()


def test_an_instance_key_shorter_than_thirty_two_bytes_refuses_to_start(
    bare_environment: pytest.MonkeyPatch,
) -> None:
    bare_environment.setenv("PRESENTATOR_SECRET_KEY", "too short")

    with pytest.raises(ValidationError, match="secret_key"):
        Settings()


@pytest.mark.usefixtures("environment")
def test_the_instance_key_is_never_shown() -> None:
    settings = Settings()

    assert _INSTANCE_KEY not in repr(settings)
    assert settings.secret_key.get_secret_value() == _INSTANCE_KEY
