"""The fetch-now hook as a git host calls it: no session, no page, no payload."""

from datetime import timedelta
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.api.hooks import hook_address
from presentator.application.decks import hash_webhook_secret
from presentator.contracts.decks import MANIFEST_FILE, SLIDES_FILE, DeckFolder, Source
from tests.api.lobby import ADMIN, NOW, GivenDecks, Lobby, a_lobby

_SOURCE_A = "alpha"
_SOURCE_B = "beta"
_WORDS_A = "the words only source A was given"
_WORDS_B = "the words only source B was given"
_A_GUESS = "guessed"
_PUSHED = "kundenfeedback"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"


def a_source(name: str, *, identifier: str, url: str) -> Source:
    return Source(
        id=identifier,
        name=name,
        url=url,
        ref="main",
        secret_location=None,
        owner_id=ADMIN,
    )


def a_pushed_folder() -> DeckFolder:
    return DeckFolder(
        name=_PUSHED,
        file_names=frozenset({MANIFEST_FILE, SLIDES_FILE}),
        title="Kundenfeedback Q3",
        changed_at=NOW - timedelta(minutes=2),
        commit=_COMMIT,
    )


def carrying_bearer(words: str) -> dict[str, str]:
    return {"authorization": f"Bearer {words}"}


def carrying_gitlab(words: str) -> dict[str, str]:
    return {"x-gitlab-token": words}


def two_sources() -> GivenDecks:
    source_a = a_source(
        _SOURCE_A,
        identifier="source-a",
        url="https://git.example.invalid/alpha.git",
    )
    source_b = a_source(
        _SOURCE_B,
        identifier="source-b",
        url="https://git.example.invalid/beta.git",
    )
    folder = a_pushed_folder()
    return GivenDecks(
        sources=(source_a, source_b),
        carried={source_a.id: (folder,), source_b.id: (folder,)},
        hook_hashes={
            source_a.name: hash_webhook_secret(_WORDS_A),
            source_b.name: hash_webhook_secret(_WORDS_B),
        },
    )


def a_lobby_with_hooks() -> Lobby:
    return a_lobby(given=two_sources())


def call_hook(
    client: TestClient,
    *,
    name: str = _SOURCE_A,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    path: str | None = None,
) -> Response:
    return client.post(
        hook_address(name) if path is None else path,
        headers=headers,
        content=content,
    )


@pytest.fixture
def hooked() -> Lobby:
    return a_lobby_with_hooks()


def test_a_call_carrying_the_sources_secret_takes_the_push_in(hooked: Lobby) -> None:
    called = call_hook(hooked.client, headers=carrying_bearer(_WORDS_A))
    hooked.set_up_admin()

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert "Kundenfeedback Q3" in hooked.client.get("/").text


def test_a_call_with_the_gitlab_header_takes_the_push_in(hooked: Lobby) -> None:
    called = call_hook(hooked.client, headers=carrying_gitlab(_WORDS_A))
    hooked.set_up_admin()

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert "Kundenfeedback Q3" in hooked.client.get("/").text


def test_the_hook_answers_a_host_that_has_no_session_and_no_page_here(
    hooked: Lobby,
) -> None:
    called = call_hook(
        hooked.client,
        headers={
            **carrying_bearer(_WORDS_A),
            "origin": "https://git.example",
        },
    )

    assert called.status_code == HTTPStatus.NO_CONTENT


def test_whatever_a_host_posts_in_its_body_changes_nothing(hooked: Lobby) -> None:
    called = call_hook(
        hooked.client,
        headers={
            **carrying_bearer(_WORDS_A),
            "content-type": "application/json",
        },
        content=b'{"repository": {"name": "somebody-elses-repository"}}',
    )
    hooked.set_up_admin()

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert "Kundenfeedback Q3" in hooked.client.get("/").text


def test_source_bs_secret_does_not_open_source_a(hooked: Lobby) -> None:
    refused = call_hook(hooked.client, headers=carrying_bearer(_WORDS_B))
    hooked.set_up_admin()

    assert (refused.status_code, refused.content) == (HTTPStatus.NOT_FOUND, b"")
    assert "Kundenfeedback Q3" not in hooked.client.get("/").text


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        pytest.param(hook_address(_SOURCE_A), None, id="nothing carried"),
        pytest.param(
            hook_address(_SOURCE_A),
            carrying_bearer(_A_GUESS),
            id="wrong words",
        ),
        pytest.param(
            hook_address(_SOURCE_A),
            {"authorization": _WORDS_A},
            id="right words, no bearer",
        ),
        pytest.param(
            hook_address("unknown"),
            carrying_bearer(_WORDS_A),
            id="unknown source",
        ),
        pytest.param(
            f"{hook_address(_SOURCE_A)}/",
            carrying_bearer(_WORDS_A),
            id="the source with a trailing slash",
        ),
        pytest.param(
            f"{hook_address(_SOURCE_A)}/refresh",
            carrying_bearer(_WORDS_A),
            id="a path below the source",
        ),
        pytest.param("/sources/", carrying_bearer(_WORDS_A), id="no source at all"),
        pytest.param(
            "/sources/fetch",
            carrying_bearer(_WORDS_A),
            id="fetch with no name",
        ),
    ],
)
def test_a_call_that_cannot_name_a_source_and_its_secret_is_refused_alike(
    hooked: Lobby,
    path: str,
    headers: dict[str, str] | None,
) -> None:
    refused = call_hook(hooked.client, path=path, headers=headers)

    assert (refused.status_code, refused.content) == (HTTPStatus.NOT_FOUND, b"")
    assert refused.headers.get("location") is None


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_reading_the_hook_address_leads_to_the_login_like_any_other(
    hooked: Lobby,
    method: str,
) -> None:
    asked = hooked.client.request(
        method,
        hook_address(_SOURCE_A),
        headers=carrying_bearer(_WORDS_A),
    )

    assert asked.status_code == HTTPStatus.FOUND
    assert asked.headers["location"] == "/login"


def test_a_source_without_a_webhook_hash_is_refused_alike_not_sent_to_login() -> None:
    source = a_source(
        _SOURCE_A,
        identifier="source-a",
        url="https://git.example.invalid/alpha.git",
    )
    lobby = a_lobby(given=GivenDecks(sources=(source,)))

    called = call_hook(lobby.client, headers=carrying_bearer(_WORDS_A))

    assert (called.status_code, called.content) == (HTTPStatus.NOT_FOUND, b"")
    assert called.headers.get("location") is None
