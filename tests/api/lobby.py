"""The lobby the deck routes are driven through: real routes over fake stores.

The deck list and the deck page arrange the same instance, so the wiring the
host does stands here once, with the doubles a test hands in.
"""

from datetime import UTC, datetime
from functools import partial
from typing import Final

from fastapi.testclient import TestClient

from presentator.adapters.catalog import ENGLISH_CATALOG, age_in_words, load_lobby_text
from presentator.api.auth import Wording, create_lobby
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.contracts.decks import DeckFolder, Source
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

NOW: Final = datetime(2026, 1, 15, 9, tzinfo=UTC)
PERSON: Final = "felix"
ADMIN: Final = "the-admin"
TEXT: LobbyText = load_lobby_text(ENGLISH_CATALOG)

_TYPED_WORDS: Final = "the words only this test types"


def a_configured_source(url: str) -> Source:
    """The one source an installation carries, owned by its admin."""
    return Source(url=url, ref="main", credential_reference=None, owner_id=ADMIN)


def a_lobby(
    *,
    folders: tuple[DeckFolder, ...] | None = None,
    source: Source | None = None,
    store: FakeDeckStore | None = None,
) -> TestClient:
    """The whole lobby over fakes, with nobody signed in yet."""
    clock = FrozenClock(instant=NOW)
    decks = Decks(
        sources=FakeSourceStore(source=source),
        folders=FakeDeckFolders(found=folders),
        store=FakeDeckStore() if store is None else store,
        # Nothing builds at this layer: a test arranges the build its deck
        # delivers from, the way it arranges the row.
        builder=FakeBuildRunner(fails=True),
        clock=clock,
    )
    # The list and the deck page read the store only; a test arranges what a
    # poll or the hook would already have taken in before anyone opened a page.
    # No folders named is not the source carrying none: it is a test that
    # populated the store itself, so the one refresh here must read nothing
    # rather than reconcile away what the test already put there.
    decks.refresh()
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
            text=TEXT,
            age_in_words=partial(age_in_words, language_tag=TEXT.language_tag),
        ),
        secure_cookies=False,
        fetch_hook=None,
    )
    return TestClient(lobby, follow_redirects=False)


def a_signed_in_lobby(
    *,
    folders: tuple[DeckFolder, ...] | None = None,
    source: Source | None = None,
    store: FakeDeckStore | None = None,
) -> TestClient:
    """The same lobby with its first admin created and their session open."""
    client = a_lobby(folders=folders, source=source, store=store)
    client.post(
        "/setup",
        data={
            "username": PERSON,
            "password": _TYPED_WORDS,
            "repeated_password": _TYPED_WORDS,
        },
    )
    return client
