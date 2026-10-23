"""A database the instance starts on, or refuses before it changes a byte."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from webauth.config import installed_web_auth_config

from presentator.adapters.identity import (
    SqliteSessionRecordStore,
    create_identity_tables,
)
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Role, User
from presentator.host import main
from presentator.host.config import ConfigurationError, load_settings
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    hand_over_session_cookies,
)
from tests.people import (
    copy_of_a_crashed_database,
    seed_user,
    write_password_schema,
)

_ADMIN = User(id="id-of-felix", username="felix", role=Role.ADMIN)


def test_an_instance_refuses_a_database_with_login_attempts_before_changing_it(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "presentator.sqlite3"
    environment.setenv("PRESENTATOR_DATABASE", str(database))
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE login_attempts (username TEXT)")
        connection.commit()
    before = database.read_bytes()
    folder_before = sorted(tmp_path.iterdir())

    with pytest.raises(ConfigurationError, match="move the database"):
        main.build_instance(load_settings())

    assert database.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == folder_before


def test_an_instance_on_a_relative_database_path_starts_again_after_its_first_start(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment.chdir(tmp_path)
    environment.setenv("PRESENTATOR_DATABASE", "presentator.sqlite3")
    main.build_instance(load_settings())

    main.build_instance(load_settings())

    assert (tmp_path / "presentator.sqlite3").is_file()


def test_an_instance_refuses_a_password_schema_only_in_the_write_ahead_log(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = copy_of_a_crashed_database(
        tmp_path / "crashed" / "presentator.sqlite3", write_password_schema
    )
    environment.setenv("PRESENTATOR_DATABASE", str(database))
    log = database.with_name(database.name + "-wal")
    main_file_before = database.read_bytes()
    log_before = log.read_bytes()

    with pytest.raises(ConfigurationError, match="move the database"):
        main.build_instance(load_settings())

    assert database.read_bytes() == main_file_before
    assert log.read_bytes() == log_before


def test_an_instance_restarts_on_a_crash_that_left_only_current_frames_in_the_log(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_ids: list[str] = []

    def commit_a_person_and_a_session(database: Path) -> None:
        create_identity_tables(database)
        seed_user(database, _ADMIN)
        now = datetime.now(UTC)
        opened = SqliteSessionRecordStore(database).create(
            _ADMIN.id,
            now=now,
            expires_at=now + SESSION_LIFETIME,
            refresh_token=None,
            id_token=SecretStr("an-id-token"),
        )
        session_ids.append(opened.id)

    database = copy_of_a_crashed_database(
        tmp_path / "crashed" / "presentator.sqlite3", commit_a_person_and_a_session
    )
    environment.setenv("PRESENTATOR_DATABASE", str(database))

    instance = main.build_instance(load_settings())

    survivor = SqliteSessionRecordStore(database).load(session_ids[0])
    assert survivor is not None
    assert survivor.user == _ADMIN
    lobby = TestClient(
        instance.lobby,
        base_url=A_BROWSERS_ADDRESS,
        follow_redirects=False,
        headers=A_BROWSERS_HEADERS,
    )
    hand_over_session_cookies(
        lobby, session_ids[0], installed_web_auth_config(instance.lobby)
    )
    assert lobby.get("/").status_code == HTTPStatus.OK
