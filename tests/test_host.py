"""The composition root, from the environment to a lobby that answers."""

import logging
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from presentator.adapters.decks import SqliteDeckStore
from presentator.adapters.identity import SqliteUserStore
from presentator.api.auth import SESSION_COOKIE
from presentator.host import main
from presentator.host.config import load_settings
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, GitRemote

if TYPE_CHECKING:
    from fastapi import FastAPI

_INSTANCE_KEY = "an instance key of at least thirty-two bytes"
_PERSON = "felix"
_TYPED_WORDS = "the words only this test types"
_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)


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


def a_real_lobby() -> TestClient:
    """The whole stack the composition root builds, on the configured database."""
    return TestClient(main.build_lobby(load_settings()), follow_redirects=False)


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


@pytest.mark.usefixtures("environment")
def test_the_real_stack_signs_a_person_in_and_out_and_keeps_the_password_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_lobby = a_real_lobby()

    with caplog.at_level(logging.DEBUG):
        created = real_lobby.post(
            "/setup",
            data={
                "username": _PERSON,
                "password": _TYPED_WORDS,
                "repeated_password": _TYPED_WORDS,
            },
        )
        assert created.status_code == HTTPStatus.SEE_OTHER
        assert _PERSON in real_lobby.get("/").text

        signed_in_cookie = real_lobby.cookies[SESSION_COOKIE]
        assert real_lobby.post("/logout").status_code == HTTPStatus.SEE_OTHER

        real_lobby.cookies.set(SESSION_COOKIE, signed_in_cookie)
        assert real_lobby.get("/").status_code == HTTPStatus.FOUND

        real_lobby.cookies.delete(SESSION_COOKIE)
        signed_in_again = real_lobby.post(
            "/login",
            data={"username": _PERSON, "password": _TYPED_WORDS},
        )
        assert signed_in_again.status_code == HTTPStatus.SEE_OTHER
        assert real_lobby.get("/").is_success

    assert _TYPED_WORDS not in caplog.text


@pytest.mark.usefixtures("environment")
def test_the_real_stack_refuses_a_login_nobody_has_an_account_for() -> None:
    real_lobby = a_real_lobby()

    refused = real_lobby.post(
        "/login",
        data={"username": "nobody", "password": _TYPED_WORDS},
    )

    assert refused.status_code == HTTPStatus.OK
    assert SESSION_COOKIE not in real_lobby.cookies


@pytest.mark.usefixtures("bare_environment")
def test_an_instance_without_a_key_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        load_settings()


def test_an_instance_key_shorter_than_thirty_two_bytes_refuses_to_start(
    bare_environment: pytest.MonkeyPatch,
) -> None:
    bare_environment.setenv("PRESENTATOR_SECRET_KEY", "too short")

    with pytest.raises(ValidationError, match="secret_key"):
        load_settings()


@pytest.mark.usefixtures("environment")
def test_the_instance_key_is_never_shown() -> None:
    settings = load_settings()

    assert _INSTANCE_KEY not in repr(settings)
    assert settings.secret_key.get_secret_value() == _INSTANCE_KEY


def a_signed_in_instance() -> TestClient:
    """The real stack with its admin created and its session open."""
    instance = a_real_lobby()
    instance.post(
        "/setup",
        data={
            "username": _PERSON,
            "password": _TYPED_WORDS,
            "repeated_password": _TYPED_WORDS,
        },
    )
    return instance


def test_the_real_stack_lists_a_deck_pushed_into_a_git_source(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_URL", remote.url)
    remote.commit_example_deck(at=_PUSHED_AT)

    listed = a_signed_in_instance().get("/").text

    assert EXAMPLE_TITLE in listed
    assert f'href="/deck/{EXAMPLE_SLUG}"' in listed


def test_a_deck_the_real_stack_took_in_belongs_to_the_instance_admin(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_URL", remote.url)
    remote.commit_example_deck(at=_PUSHED_AT)
    database = tmp_path / "presentator.sqlite3"

    a_signed_in_instance().get("/")

    kept = SqliteDeckStore(database=database).all()
    admin = SqliteUserStore(database).first_admin()
    assert admin is not None
    assert [(deck.slug, deck.owner_id) for deck in kept] == [(EXAMPLE_SLUG, admin.id)]


@pytest.mark.usefixtures("environment")
def test_an_instance_without_a_source_shows_the_empty_list() -> None:
    listed = a_signed_in_instance().get("/")

    assert listed.status_code == HTTPStatus.OK
    assert "<table" not in listed.text
