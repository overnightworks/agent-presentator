"""The refresh token a sign-in stores with its session, and where it never shows."""

import logging
import sqlite3
import traceback
from contextlib import closing
from http import HTTPStatus
from pathlib import Path

import pytest
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME

from presentator.api.auth import LOGOUT_PATH
from tests.sign_in_support import Scene, a_scene, cookie_names

_MARKER = "rt-MARKER"
_LOGGERS = ("uvicorn.error", "webauth", "presentator")


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return a_scene(tmp_path)


def test_the_sign_ins_refresh_token_is_stored_with_its_session(scene: Scene) -> None:
    scene.issuer.issue_refresh_token(_MARKER)

    scene.sign_in()

    assert scene.stored_tokens() == [_MARKER]


def test_a_sign_in_without_a_refresh_token_succeeds_and_stores_none(
    scene: Scene,
) -> None:
    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert scene.stored_tokens() == [None]


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("rt\x01control", id="control character"),
        pytest.param("rt-MARKERé", id="non-ascii letter"),
    ],
)
def test_a_refresh_token_outside_printable_ascii_fails_the_login_with_no_session(
    scene: Scene, token: str
) -> None:
    scene.issuer.issue_refresh_token(token)

    page = scene.sign_in()

    assert page.status_code == HTTPStatus.FORBIDDEN
    assert "<h1>Login failed</h1>" in page.text
    assert scene.count("sessions") == 0
    assert DEFAULT_SESSION_COOKIE_NAME not in " ".join(cookie_names(page))


def test_a_refresh_token_with_a_space_is_printable_ascii_and_is_stored(
    scene: Scene,
) -> None:
    scene.issuer.issue_refresh_token("rt with space")

    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert scene.stored_tokens() == ["rt with space"]


def test_a_sign_in_over_an_older_session_replaces_its_row_and_its_token(
    scene: Scene,
) -> None:
    browser = scene.browser()
    scene.issuer.issue_refresh_token("rt-OLD")
    scene.sign_in(browser)
    (old_id,) = scene.stored_sessions()
    scene.issuer.issue_refresh_token("rt-NEW")

    scene.sign_in(browser)

    sessions = scene.stored_sessions()
    assert old_id not in sessions
    assert list(sessions.values()) == ["rt-NEW"]


def test_logging_out_deletes_the_session_row_with_its_token(scene: Scene) -> None:
    scene.issuer.issue_refresh_token(_MARKER)
    scene.sign_in()

    scene.lobby.client.post(LOGOUT_PATH)

    assert scene.stored_tokens() == []


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Every record of the loggers a sign-in can reach, at DEBUG."""
    caplog.set_level(logging.DEBUG)
    for name in _LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


def _everything_logged(log: pytest.LogCaptureFixture) -> str:
    """Each record's message with its exception and stack, chains included."""
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in log.records)


def test_a_sign_in_that_stores_the_token_leaks_it_to_neither_page_nor_log(
    scene: Scene, log: pytest.LogCaptureFixture
) -> None:
    scene.issuer.issue_refresh_token(_MARKER)

    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert scene.stored_tokens() == [_MARKER]
    assert _MARKER not in answer.text + str(answer.headers)
    assert _MARKER not in _everything_logged(log)


def test_a_refused_refresh_token_leaks_to_neither_page_nor_log(
    scene: Scene, log: pytest.LogCaptureFixture
) -> None:
    scene.issuer.issue_refresh_token(_MARKER + "é")

    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.FORBIDDEN
    assert "<h1>Login failed</h1>" in answer.text
    assert _MARKER not in answer.text + str(answer.headers)
    assert _MARKER not in _everything_logged(log)


def test_a_store_failure_holding_the_token_leaks_it_to_no_error_and_no_log(
    scene: Scene, log: pytest.LogCaptureFixture
) -> None:
    scene.issuer.issue_refresh_token(_MARKER)
    with closing(sqlite3.connect(scene.database)) as connection:
        connection.execute("DROP TABLE sessions")

    with pytest.raises(sqlite3.Error) as failure:
        scene.sign_in()

    assert _MARKER not in "".join(traceback.format_exception(failure.value))
    assert _MARKER not in _everything_logged(log)
