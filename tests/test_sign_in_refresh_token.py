"""The refresh token a sign-in stores with its session, and where it never shows."""

import json
import logging
import sqlite3
from contextlib import closing
from http import HTTPStatus
from pathlib import Path

import pytest
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME
from webauth.oidc import TokenEndpointAnswer, TokenRequest, TokenRequestPost

from presentator.api.auth import LOGOUT_PATH
from tests.leak_support import everything_shown
from tests.sign_in_support import Scene, a_scene, cookie_names

_MARKER = "rt-MARKER"
_LOGGERS = ("uvicorn.error", "webauth", "presentator")


@pytest.fixture
def issued_id_tokens() -> list[str]:
    """Every ID token the issuer's token endpoint hands out, as it is handed out."""
    return []


@pytest.fixture
def scene(tmp_path: Path, issued_id_tokens: list[str]) -> Scene:
    def recording(answer_the_request: TokenRequestPost) -> TokenRequestPost:
        def record(request: TokenRequest) -> TokenEndpointAnswer:
            answer = answer_the_request(request)
            id_token = json.loads(answer.body).get("id_token")
            if id_token is not None:
                issued_id_tokens.append(id_token)
            return answer

        return record

    return a_scene(tmp_path, token_requests=recording)


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


def test_a_sign_in_that_stores_the_token_leaks_it_to_neither_page_nor_log(
    scene: Scene, log: pytest.LogCaptureFixture
) -> None:
    scene.issuer.issue_refresh_token(_MARKER)

    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.SEE_OTHER
    assert scene.stored_tokens() == [_MARKER]
    assert _MARKER not in answer.text + str(answer.headers)
    assert _MARKER not in everything_shown(log.records)


def test_a_refused_refresh_token_leaks_to_neither_page_nor_log(
    scene: Scene, log: pytest.LogCaptureFixture
) -> None:
    scene.issuer.issue_refresh_token(_MARKER + "é")

    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.FORBIDDEN
    assert "<h1>Login failed</h1>" in answer.text
    assert _MARKER not in answer.text + str(answer.headers)
    assert _MARKER not in everything_shown(log.records)


def test_a_store_failure_holding_the_token_leaks_it_to_no_error_and_no_log(
    scene: Scene, log: pytest.LogCaptureFixture
) -> None:
    scene.issuer.issue_refresh_token(_MARKER)
    with closing(sqlite3.connect(scene.database)) as connection:
        connection.execute("DROP TABLE sessions")

    with pytest.raises(sqlite3.Error) as failure:
        scene.sign_in()

    assert _MARKER not in everything_shown(errors=[failure.value])
    assert _MARKER not in everything_shown(log.records)


def test_a_store_failure_holding_the_token_answers_500_without_it(
    scene: Scene,
) -> None:
    scene.issuer.issue_refresh_token(_MARKER)
    with closing(sqlite3.connect(scene.database)) as connection:
        connection.execute("DROP TABLE sessions")

    answer = scene.sign_in(scene.browser(raise_server_exceptions=False))

    assert answer.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert _MARKER not in answer.text + str(answer.headers)


def _the_id_tokens_and_their_claims(tokens: list[str]) -> list[str]:
    assert tokens
    return [*tokens, *(token.split(".")[1] for token in tokens)]


def test_a_sign_in_that_stores_the_id_token_leaks_it_to_neither_page_nor_log(
    scene: Scene,
    log: pytest.LogCaptureFixture,
    issued_id_tokens: list[str],
) -> None:
    """#428 cell 11: stored sign-in."""
    answer = scene.sign_in()

    (stored,) = scene.stored_id_tokens()
    assert stored in issued_id_tokens
    for secret in _the_id_tokens_and_their_claims(issued_id_tokens):
        assert secret not in answer.text + str(answer.headers)
        assert secret not in everything_shown(log.records)


def test_a_store_failure_holding_the_id_token_leaks_it_to_no_error_and_no_log(
    scene: Scene,
    log: pytest.LogCaptureFixture,
    issued_id_tokens: list[str],
) -> None:
    """#428 cell 11: store failure."""
    with closing(sqlite3.connect(scene.database)) as connection:
        connection.execute("DROP TABLE sessions")

    with pytest.raises(sqlite3.Error) as failure:
        scene.sign_in()
    answer = scene.sign_in(scene.browser(raise_server_exceptions=False))

    assert answer.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    for secret in _the_id_tokens_and_their_claims(issued_id_tokens):
        assert secret not in everything_shown(errors=[failure.value])
        assert secret not in answer.text + str(answer.headers)
        assert secret not in everything_shown(log.records)


def test_a_refused_sign_in_leaks_the_id_token_to_neither_page_nor_log(
    scene: Scene,
    log: pytest.LogCaptureFixture,
    issued_id_tokens: list[str],
) -> None:
    """#428 cell 11: refused sign-in."""
    scene.issuer.issue_refresh_token(_MARKER + "é")

    answer = scene.sign_in()

    assert answer.status_code == HTTPStatus.FORBIDDEN
    for secret in _the_id_tokens_and_their_claims(issued_id_tokens):
        assert secret not in answer.text + str(answer.headers)
        assert secret not in everything_shown(log.records)


def test_a_logout_leaks_the_id_token_to_no_log(
    scene: Scene,
    log: pytest.LogCaptureFixture,
    issued_id_tokens: list[str],
) -> None:
    """#428 cell 11: logout; its page posts the hint to the issuer by design."""
    scene.sign_in()

    logged_out = scene.lobby.client.post(LOGOUT_PATH)

    assert logged_out.status_code == HTTPStatus.OK
    assert scene.count("sessions") == 0
    for secret in _the_id_tokens_and_their_claims(issued_id_tokens):
        assert secret not in everything_shown(log.records)


def test_a_refused_logout_leaks_the_id_token_to_neither_page_nor_log(
    scene: Scene,
    log: pytest.LogCaptureFixture,
    issued_id_tokens: list[str],
) -> None:
    """#428 cell 11: refused logout."""
    scene.sign_in()

    refused = scene.lobby.client.post(
        LOGOUT_PATH,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://another.example",
            "sec-fetch-site": "cross-site",
        },
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert scene.count("sessions") == 1
    for secret in _the_id_tokens_and_their_claims(issued_id_tokens):
        assert secret not in refused.text + str(refused.headers)
        assert secret not in everything_shown(log.records)
