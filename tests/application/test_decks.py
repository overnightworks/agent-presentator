"""What the lobby calls a deck, and the order it hands the list over in."""

from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest

from presentator.application.decks import Decks
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
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


def a_folder(
    name: str,
    *,
    title: str | None = "A talk",
    file_names: frozenset[str] = _A_DECK,
    changed_ago: timedelta = timedelta(minutes=2),
) -> DeckFolder:
    return DeckFolder(
        name=name,
        file_names=file_names,
        title=title,
        changed_at=_NOW - changed_ago,
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


def test_a_folder_the_source_no_longer_carries_leaves_the_list() -> None:
    mirror = FakeDeckFolders(
        found=(a_folder("alter-vortrag"), a_folder("kundenfeedback")),
    )
    decks = decks_over(mirror=mirror)
    refreshed(decks)

    mirror.found = (a_folder("kundenfeedback"),)
    after_the_delete = refreshed(decks)

    assert [deck.slug for deck in after_the_delete] == ["kundenfeedback"]


def test_a_folder_pushed_again_is_the_same_deck_with_the_same_owner() -> None:
    mirror = FakeDeckFolders(found=(a_folder("kundenfeedback", title="Feedback"),))
    store = FakeDeckStore()
    decks = decks_over(mirror=mirror, store=store)
    refreshed(decks)

    mirror.found = ()
    while_it_was_gone = refreshed(decks)
    mirror.found = (a_folder("kundenfeedback", title="Feedback"),)
    after_it_came_back = refreshed(decks)

    assert while_it_was_gone == ()
    assert [deck.slug for deck in after_it_came_back] == ["kundenfeedback"]
    assert [deck.owner_id for deck in store.all()] == [_OWNER]


def test_a_source_that_cannot_be_read_leaves_every_deck_listed() -> None:
    mirror = FakeDeckFolders(found=(a_folder("kundenfeedback"),))
    decks = decks_over(mirror=mirror)
    refreshed(decks)

    mirror.found = None
    while_the_source_was_unreadable = refreshed(decks)

    assert [deck.slug for deck in while_the_source_was_unreadable] == ["kundenfeedback"]
