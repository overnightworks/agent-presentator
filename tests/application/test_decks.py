"""What the lobby calls a deck, which deck it builds, and what it then shows."""

from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest

from presentator.application.decks import Decks
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    DeckFolder,
    DeckPage,
    ListedDeck,
    Source,
)
from presentator.ports.decks import DeckFolders
from tests.application.fakes import (
    BUILDS_ROOT,
    PATIENCE,
    FakeBuildRunner,
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
_A_LATER_COMMIT = "b7c1d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"


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
    builder: FakeBuildRunner | None = None,
    clock: FrozenClock | None = None,
) -> Decks:
    return Decks(
        sources=FakeSourceStore(source=source),
        folders=FakeDeckFolders(found=folders) if mirror is None else mirror,
        store=FakeDeckStore() if store is None else store,
        builder=FakeBuildRunner() if builder is None else builder,
        clock=FrozenClock(instant=_NOW) if clock is None else clock,
    )


def refreshed(decks: Decks) -> tuple[ListedDeck, ...]:
    """What the list shows once the source has been taken in."""
    decks.refresh()
    return decks.listed()


def page_of(decks: Decks, slug: str) -> DeckPage:
    """That deck's page, for a test that already knows the deck is there."""
    page = decks.page(slug)
    assert page is not None
    return page


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


@pytest.mark.parametrize(
    "name",
    ["..", "a/b"],
    ids=["one folder up", "a nested path"],
)
def test_a_folder_that_is_no_folders_own_name_is_neither_listed_nor_built(
    name: str,
) -> None:
    builder = FakeBuildRunner()
    decks = decks_over(a_folder(name), builder=builder)

    decks.refresh()

    assert decks.listed() == ()
    assert builder.built == []


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

    page = page_of(decks, "kundenfeedback")

    assert (page.slug, page.title) == ("kundenfeedback", "Kundenfeedback Q3")
    assert page.source == _SOURCE.url
    assert page.commit == _SHORT_COMMIT


def test_a_slug_no_folder_carries_has_no_page_no_talk_and_no_pdf() -> None:
    decks = decks_over(a_folder("kundenfeedback"))
    decks.refresh()

    assert decks.page("never-pushed") is None
    assert decks.built_talk("never-pushed") is None
    assert decks.exported_pdf("never-pushed") is None


def test_a_deck_whose_commit_was_never_built_is_built_and_then_delivered() -> None:
    clock = FrozenClock(instant=_NOW)
    decks = decks_over(a_folder("kundenfeedback"), clock=clock)

    decks.refresh()
    clock.advance(by=timedelta(minutes=12))

    assert page_of(decks, "kundenfeedback").built_ago == timedelta(minutes=12)
    talk = decks.built_talk("kundenfeedback")
    export = decks.exported_pdf("kundenfeedback")
    assert talk is not None
    assert export is not None
    assert talk.is_relative_to(BUILDS_ROOT)
    assert export.is_relative_to(BUILDS_ROOT)


def test_a_deck_already_built_at_its_commit_is_not_built_again() -> None:
    builder = FakeBuildRunner()
    decks = decks_over(a_folder("kundenfeedback"), builder=builder)

    decks.refresh()
    decks.refresh()

    assert builder.built == ["kundenfeedback"]


def test_a_push_builds_the_deck_it_changed_and_leaves_the_others_alone() -> None:
    builder = FakeBuildRunner()
    mirror = FakeDeckFolders(
        found=(a_folder("kundenfeedback"), a_folder("knowledge-fabric")),
    )
    decks = decks_over(mirror=mirror, builder=builder)
    decks.refresh()

    mirror.found = (
        a_folder("kundenfeedback", commit=_A_LATER_COMMIT),
        a_folder("knowledge-fabric"),
    )
    decks.refresh()

    assert builder.built == ["kundenfeedback", "knowledge-fabric", "kundenfeedback"]


def test_a_build_that_failed_leaves_the_talk_that_stands_standing() -> None:
    builder = FakeBuildRunner()
    mirror = FakeDeckFolders(found=(a_folder("kundenfeedback"),))
    decks = decks_over(mirror=mirror, builder=builder)
    decks.refresh()
    standing = decks.built_talk("kundenfeedback")
    exported = decks.exported_pdf("kundenfeedback")
    built_when = page_of(decks, "kundenfeedback").built_ago

    builder.fails = True
    mirror.found = (a_folder("kundenfeedback", commit=_A_LATER_COMMIT),)
    decks.refresh()

    assert decks.built_talk("kundenfeedback") == standing
    assert decks.exported_pdf("kundenfeedback") == exported
    standing_page = page_of(decks, "kundenfeedback")
    assert standing_page.built_ago == built_when
    assert standing_page.commit == _SHORT_COMMIT


def test_a_deck_nothing_could_build_delivers_nothing_and_says_so() -> None:
    decks = decks_over(a_folder("kundenfeedback"), builder=FakeBuildRunner(fails=True))

    decks.refresh()

    assert page_of(decks, "kundenfeedback").built_ago is None
    assert decks.built_talk("kundenfeedback") is None
    assert decks.exported_pdf("kundenfeedback") is None


def test_a_build_that_wrote_outside_the_builds_root_becomes_no_address() -> None:
    builder = FakeBuildRunner(writes_outside_the_root=True)
    decks = decks_over(a_folder("kundenfeedback"), builder=builder)

    decks.refresh()

    assert builder.built == ["kundenfeedback"]
    assert page_of(decks, "kundenfeedback").built_ago is None
    assert decks.built_talk("kundenfeedback") is None
    assert decks.exported_pdf("kundenfeedback") is None


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
    decks = decks_over(a_folder(slug))
    decks.refresh()

    assert decks.exported_pdf(slug) is None


def test_taking_a_pushed_deck_in_again_leaves_what_it_delivers_standing() -> None:
    decks = decks_over(a_folder("kundenfeedback"))
    decks.refresh()
    standing = decks.built_talk("kundenfeedback")
    exported = decks.exported_pdf("kundenfeedback")

    decks.refresh()

    assert decks.built_talk("kundenfeedback") == standing
    assert decks.exported_pdf("kundenfeedback") == exported


def test_a_deck_page_names_no_source_while_none_is_configured() -> None:
    store = FakeDeckStore()
    decks_over(a_folder("kundenfeedback"), store=store).refresh()

    page = page_of(decks_over(source=None, store=store), "kundenfeedback")

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
