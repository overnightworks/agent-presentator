"""What the lobby calls a deck, and the order it hands the list over in."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread

import pytest

from presentator.application.decks import Decks
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    Deck,
    DeckFolder,
    ListedDeck,
    Source,
)
from presentator.ports.decks import DeckFolders
from tests.application.fakes import (
    PATIENCE,
    FakeDeckFolders,
    FakeDeckStore,
    FakeSourceStore,
    FrozenClock,
    HeldDeckFolders,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_OWNER = "the account that set the instance up"
_SOURCE = Source(
    url="git@example.invalid:decks.git",
    ref="main",
    credential_reference=None,
    owner_id=_OWNER,
)
_A_DECK = frozenset({MANIFEST_FILE, SLIDES_FILE})
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_SHORT_COMMIT = "a3f19c2"
_BUILT_TALK = Path("/var/lib/presentator/builds/kundenfeedback/a3f19c2")
_EXPORTED_PDF = Path("/var/lib/presentator/exports/kundenfeedback/a3f19c2.pdf")


def a_folder(
    name: str,
    *,
    title: str | None = "A talk",
    file_names: frozenset[str] = _A_DECK,
    changed_ago: timedelta = timedelta(minutes=2),
    commit: str = _COMMIT,
) -> DeckFolder:
    return DeckFolder(
        name=name,
        file_names=file_names,
        title=title,
        changed_at=_NOW - changed_ago,
        commit=commit,
    )


def decks_over(
    *folders: DeckFolder,
    source: Source | None = _SOURCE,
    mirror: DeckFolders | None = None,
    store: FakeDeckStore | None = None,
) -> Decks:
    return Decks(
        sources=FakeSourceStore(source=source),
        folders=FakeDeckFolders(found=folders) if mirror is None else mirror,
        store=FakeDeckStore() if store is None else store,
        clock=FrozenClock(instant=_NOW),
    )


def a_store_holding_an_export(slug: str) -> FakeDeckStore:
    """A store that would hand a PDF over for that slug, however it is written."""
    store = FakeDeckStore()
    store.put(
        Deck(
            slug=slug,
            title="Kundenfeedback",
            changed_at=_NOW,
            owner_id=_OWNER,
            commit=_COMMIT,
            active_build=None,
            pdf_export=None,
        ),
    )
    store.put_pdf_export(slug, file=_EXPORTED_PDF)
    return store


def refreshed(decks: Decks) -> tuple[ListedDeck, ...]:
    """What the list shows once the source has been taken in."""
    decks.refresh()
    return decks.listed()


def test_the_most_recently_changed_deck_is_listed_first() -> None:
    decks = decks_over(
        a_folder("older", changed_ago=timedelta(days=6)),
        a_folder("newer", changed_ago=timedelta(minutes=2)),
    )

    listed = refreshed(decks)

    assert [deck.slug for deck in listed] == ["newer", "older"]
    assert listed[0].age == timedelta(minutes=2)


@pytest.mark.parametrize(
    "folder",
    [
        a_folder("without-slides", file_names=frozenset({MANIFEST_FILE})),
        a_folder("without-a-manifest", title=None),
    ],
    ids=["no slides", "no manifest"],
)
def test_a_folder_without_both_files_is_no_deck(folder: DeckFolder) -> None:
    assert refreshed(decks_over(folder)) == ()


def test_the_folder_name_stays_the_address_when_the_title_changes() -> None:
    mirror = FakeDeckFolders(found=(a_folder("knowledge-fabric", title="Fabric"),))
    decks = decks_over(mirror=mirror)
    first = refreshed(decks)

    mirror.found = (a_folder("knowledge-fabric", title="Fabric v2"),)
    renamed = refreshed(decks)

    assert [deck.slug for deck in first] == ["knowledge-fabric"]
    assert [(deck.slug, deck.title) for deck in renamed] == [
        ("knowledge-fabric", "Fabric v2"),
    ]


def test_a_deck_belongs_to_the_owner_of_the_source_it_came_from() -> None:
    store = FakeDeckStore()

    decks_over(a_folder("kundenfeedback"), store=store).refresh()

    assert [deck.owner_id for deck in store.all()] == [_OWNER]


def test_without_a_configured_source_there_is_no_list_and_no_address() -> None:
    decks = decks_over(a_folder("kundenfeedback"), source=None)

    assert refreshed(decks) == ()
    assert decks.source_address() is None


def test_the_empty_list_can_name_the_configured_address() -> None:
    assert decks_over().source_address() == _SOURCE.url


def test_a_deck_page_names_the_title_the_source_and_the_short_commit() -> None:
    decks = decks_over(a_folder("kundenfeedback", title="Kundenfeedback Q3"))
    decks.refresh()

    page = decks.page("kundenfeedback")

    assert page is not None
    assert (page.slug, page.title) == ("kundenfeedback", "Kundenfeedback Q3")
    assert page.source == _SOURCE.url
    assert page.commit == _SHORT_COMMIT
    assert not page.built
    assert not page.exported


def test_a_slug_no_folder_carries_has_no_page_no_talk_and_no_pdf() -> None:
    decks = decks_over(a_folder("kundenfeedback"))
    decks.refresh()

    assert decks.page("never-pushed") is None
    assert decks.built_talk("never-pushed") is None
    assert decks.exported_pdf("never-pushed") is None


def test_a_built_deck_offers_its_talk_and_says_it_is_ready() -> None:
    store = FakeDeckStore()
    decks = decks_over(a_folder("kundenfeedback"), store=store)
    decks.refresh()

    store.put_active_build("kundenfeedback", directory=_BUILT_TALK)
    page = decks.page("kundenfeedback")

    assert page is not None
    assert page.built
    assert decks.built_talk("kundenfeedback") == _BUILT_TALK


def test_an_exported_deck_hands_its_pdf_over_and_says_so_on_its_page() -> None:
    store = FakeDeckStore()
    decks = decks_over(a_folder("kundenfeedback"), store=store)
    decks.refresh()

    store.put_pdf_export("kundenfeedback", file=_EXPORTED_PDF)
    page = decks.page("kundenfeedback")

    assert page is not None
    assert page.exported
    assert decks.exported_pdf("kundenfeedback") == _EXPORTED_PDF


@pytest.mark.parametrize(
    "slug",
    [
        "deck/with-a-separator",
        "deck\\with-a-separator",
        'deck"with-a-quote',
        "deck\nwith-a-line-break",
        "..",
        "",
    ],
    ids=["separator", "backslash", "quote", "line break", "one folder up", "nothing"],
)
def test_a_slug_that_is_no_folders_own_name_hands_over_no_pdf(slug: str) -> None:
    decks = decks_over(store=a_store_holding_an_export(slug))

    assert decks.exported_pdf(slug) is None


def test_taking_a_pushed_deck_in_again_leaves_what_it_delivers_standing() -> None:
    store = FakeDeckStore()
    decks = decks_over(a_folder("kundenfeedback"), store=store)
    decks.refresh()
    store.put_active_build("kundenfeedback", directory=_BUILT_TALK)
    store.put_pdf_export("kundenfeedback", file=_EXPORTED_PDF)

    decks.refresh()

    assert decks.built_talk("kundenfeedback") == _BUILT_TALK
    assert decks.exported_pdf("kundenfeedback") == _EXPORTED_PDF


def test_a_deck_page_names_no_source_while_none_is_configured() -> None:
    store = FakeDeckStore()
    decks_over(a_folder("kundenfeedback"), store=store).refresh()

    page = decks_over(source=None, store=store).page("kundenfeedback")

    assert page is not None
    assert page.source is None
    assert page.commit == _SHORT_COMMIT


def test_the_list_shows_the_last_refresh_and_reads_no_source() -> None:
    mirror = FakeDeckFolders(found=(a_folder("kundenfeedback"),))
    decks = decks_over(mirror=mirror)

    before_any_refresh = decks.listed()
    decks.refresh()
    mirror.found = (a_folder("pushed-after-the-refresh"),)

    assert before_any_refresh == ()
    assert [deck.slug for deck in decks.listed()] == ["kundenfeedback"]


def test_a_flood_of_refreshes_takes_the_source_in_once_at_a_time() -> None:
    held = HeldDeckFolders(found=(a_folder("kundenfeedback"),))
    decks = decks_over(mirror=held)
    first = Thread(target=decks.refresh)
    flood = [Thread(target=decks.refresh) for _ in range(4)]

    first.start()
    assert held.entered.wait(PATIENCE.total_seconds())
    for asking_again in flood:
        asking_again.start()
    for asking_again in flood:
        asking_again.join(PATIENCE.total_seconds())
    reads_while_one_ran = held.reads

    held.release.set()
    first.join(PATIENCE.total_seconds())
    decks.refresh()

    assert reads_while_one_ran == 1
    assert held.at_once == 1
    assert held.reads == reads_while_one_ran + 1
    assert [deck.slug for deck in decks.listed()] == ["kundenfeedback"]
