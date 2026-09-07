"""The fetch-now hook as a git host calls it: no session, no page, no payload."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.adapters.catalog import ENGLISH_CATALOG, age_in_words, load_lobby_text
from presentator.api.auth import Wording, create_lobby
from presentator.api.hooks import HOOKS_PATH, fetch_hook
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.contracts.decks import MANIFEST_FILE, SLIDES_FILE, DeckFolder, Source
from presentator.contracts.text import LobbyText
from tests.application.fakes import (
    CountingIdentifierFactory,
    FakeBuildRunner,
    FakeDeckFolders,
    FakeDeckStore,
    FakeLoginAttemptStore,
    FakeSessionRecordStore,
    FakeSourceStore,
    FakeUserStore,
    FrozenClock,
    MarkingCookieSigner,
    ReversibleHasher,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_SOURCE_NAME = "talks"
_WHAT_THE_HOST_CARRIES = "the words only this source's host was given"
_A_GUESS = "guessed"
_PUSHED = "kundenfeedback"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_TEXT: LobbyText = load_lobby_text(ENGLISH_CATALOG)


@dataclass(frozen=True, slots=True, kw_only=True)
class Hooked:
    """A lobby carrying a fetch hook, and the store that hook fills."""

    client: TestClient
    store: FakeDeckStore

    def call_hook(
        self,
        *,
        source: str = _SOURCE_NAME,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> Response:
        return self.client.post(
            f"{HOOKS_PATH}/{source}",
            headers=headers,
            content=content,
        )

    def slugs(self) -> list[str]:
        return [deck.slug for deck in self.store.all()]


def a_pushed_folder() -> DeckFolder:
    return DeckFolder(
        name=_PUSHED,
        file_names=frozenset({MANIFEST_FILE, SLIDES_FILE}),
        title="Kundenfeedback Q3",
        changed_at=_NOW - timedelta(minutes=2),
        commit=_COMMIT,
    )


def a_lobby_with_a_hook(*, armed: bool = True) -> Hooked:
    """The lobby the host composes: its pages, and the hook while one arms it."""
    clock = FrozenClock(instant=_NOW)
    store = FakeDeckStore()
    decks = Decks(
        sources=FakeSourceStore(
            source=Source(
                url="git@example.invalid:decks.git",
                ref="main",
                credential_reference=None,
                owner_id="the-admin",
            ),
        ),
        folders=FakeDeckFolders(found=(a_pushed_folder(),)),
        store=store,
        builder=FakeBuildRunner(),
        clock=clock,
    )
    lobby = create_lobby(
        identity=Identity(
            users=FakeUserStore(),
            sessions=FakeSessionRecordStore(),
            attempts=FakeLoginAttemptStore(),
            hasher=ReversibleHasher(),
            clock=clock,
            identifiers=CountingIdentifierFactory(),
            cookies=MarkingCookieSigner(),
        ),
        decks=decks,
        wording=Wording(
            text=_TEXT,
            age_in_words=partial(age_in_words, language_tag=_TEXT.language_tag),
        ),
        secure_cookies=False,
        fetch_hook=(
            fetch_hook(
                decks=decks,
                source=_SOURCE_NAME,
                secret=_WHAT_THE_HOST_CARRIES,
            )
            if armed
            else None
        ),
    )
    return Hooked(client=TestClient(lobby, follow_redirects=False), store=store)


def carrying(words: str) -> dict[str, str]:
    return {"authorization": f"Bearer {words}"}


@pytest.fixture
def hooked() -> Hooked:
    return a_lobby_with_a_hook()


def test_a_call_carrying_the_sources_secret_takes_the_push_in(hooked: Hooked) -> None:
    called = hooked.call_hook(headers=carrying(_WHAT_THE_HOST_CARRIES))

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert hooked.slugs() == [_PUSHED]


def test_the_hook_answers_a_host_that_has_no_session_and_no_page_here(
    hooked: Hooked,
) -> None:
    called = hooked.call_hook(
        headers={
            **carrying(_WHAT_THE_HOST_CARRIES),
            "origin": "https://git.example",
        },
    )

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert hooked.slugs() == [_PUSHED]


def test_whatever_a_host_posts_in_its_body_changes_nothing(hooked: Hooked) -> None:
    called = hooked.call_hook(
        headers={
            **carrying(_WHAT_THE_HOST_CARRIES),
            "content-type": "application/json",
        },
        content=b'{"repository": {"name": "somebody-elses-repository"}}',
    )

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert hooked.slugs() == [_PUSHED]


@pytest.mark.parametrize(
    ("source", "headers"),
    [
        pytest.param(_SOURCE_NAME, None, id="nothing carried"),
        pytest.param(_SOURCE_NAME, carrying(_A_GUESS), id="wrong words"),
        pytest.param(
            _SOURCE_NAME,
            {"authorization": _WHAT_THE_HOST_CARRIES},
            id="right words, no bearer",
        ),
        pytest.param(
            "another-source",
            carrying(_WHAT_THE_HOST_CARRIES),
            id="unknown source",
        ),
        pytest.param(
            f"{_SOURCE_NAME}/",
            carrying(_WHAT_THE_HOST_CARRIES),
            id="the source with a trailing slash",
        ),
        pytest.param(
            f"{_SOURCE_NAME}/refresh",
            carrying(_WHAT_THE_HOST_CARRIES),
            id="a path below the source",
        ),
        pytest.param("", carrying(_WHAT_THE_HOST_CARRIES), id="no source at all"),
    ],
)
def test_a_call_that_cannot_name_a_source_and_its_secret_is_refused_alike(
    hooked: Hooked,
    source: str,
    headers: dict[str, str] | None,
) -> None:
    refused = hooked.call_hook(source=source, headers=headers)

    assert (refused.status_code, refused.content) == (HTTPStatus.NOT_FOUND, b"")
    assert hooked.slugs() == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_reading_the_hook_address_leads_to_the_login_like_any_other(
    hooked: Hooked,
    method: str,
) -> None:
    asked = hooked.client.request(
        method,
        f"{HOOKS_PATH}/{_SOURCE_NAME}",
        headers=carrying(_WHAT_THE_HOST_CARRIES),
    )

    assert asked.status_code == HTTPStatus.FOUND
    assert asked.headers["location"] == "/login"
    assert hooked.slugs() == []


def test_without_a_secret_the_hook_address_is_no_address() -> None:
    unarmed = a_lobby_with_a_hook(armed=False)

    called = unarmed.call_hook(headers=carrying(_WHAT_THE_HOST_CARRIES))

    assert called.status_code == HTTPStatus.FOUND
    assert called.headers["location"] == "/login"
    assert unarmed.slugs() == []
