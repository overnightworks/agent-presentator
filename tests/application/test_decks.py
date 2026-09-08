"""What the lobby calls a deck, which deck it builds, and what it then shows."""

import hashlib
import logging
import string
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest

from presentator.application.decks import (
    AddedSource,
    Decks,
    SourceRefusal,
    access_kind_of,
)
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    AccessKind,
    BuildAttempt,
    BuildOutcome,
    Deck,
    DeckFolder,
    DeckPage,
    DeckState,
    ListedDeck,
    ListedSource,
    SecretLocation,
    Source,
    SourceRun,
    SourceRunFailure,
    SourceRunOutcome,
    SourceState,
    SourceWrite,
)
from presentator.ports.decks import DeckFolders
from tests.application.fakes import (
    BUILDS_ROOT,
    PATIENCE,
    FakeBuildRunner,
    FakeDeckFolders,
    FakeDeckStore,
    FakeSourceRunStore,
    FakeSourceStore,
    FrozenClock,
    HeldDeckFolders,
)

_NOW = datetime(2026, 1, 15, 9, tzinfo=UTC)
_OWNER = "the account that set the instance up"


def a_source(name: str, *, identifier: str) -> Source:
    return Source(
        id=identifier,
        name=name,
        url=f"git@example.invalid:{name}.git",
        ref="main",
        secret_location=None,
        owner_id=_OWNER,
    )


_SOURCE = a_source("decks", identifier="the-configured-source")
_ANOTHER_SOURCE = a_source("talks", identifier="a-second-source")
_A_DECK = frozenset({MANIFEST_FILE, SLIDES_FILE})
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_SHORT_COMMIT = "a3f19c2"
_A_LATER_COMMIT = "b7c1d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"
_SHORT_LATER_COMMIT = "b7c1d9e"
# Long enough that no test's build reaches it by accident, so an attempt is
# only ever given up on where a test says so.
_BUILD_BOUND = timedelta(minutes=5)
_WHAT_THE_TOOLCHAIN_SAID = "slides.md:41:3 Unexpected token in frontmatter"


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


def carrying(*folders: DeckFolder, source: Source = _SOURCE) -> FakeDeckFolders:
    """A mirror in which that source carries exactly those folders."""
    return FakeDeckFolders(carried={source.id: folders})


def having(*sources: Source, seeds: Source | None = None) -> FakeSourceStore:
    """The sources an instance already has, and the one its seed would write."""
    return FakeSourceStore(sources=list(sources), seeds=seeds)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecksFakes:
    """The fakes a `decks_over` scenario runs against, besides source and mirror.

    Grouped into one value so `decks_over` keeps five parameters; a test names
    only the fake it cares about, and every field it leaves unnamed gets its
    own fresh instance rather than one every other test would share.
    """

    store: FakeDeckStore = field(default_factory=FakeDeckStore)
    builder: FakeBuildRunner = field(default_factory=FakeBuildRunner)
    source_runs: FakeSourceRunStore = field(default_factory=FakeSourceRunStore)
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(instant=_NOW))


def decks_over(
    *folders: DeckFolder,
    sources: FakeSourceStore | None = None,
    mirror: DeckFolders | None = None,
    fakes: DecksFakes | None = None,
) -> Decks:
    resolved = DecksFakes() if fakes is None else fakes
    return Decks(
        sources=having(_SOURCE) if sources is None else sources,
        folders=carrying(*folders) if mirror is None else mirror,
        store=resolved.store,
        builder=resolved.builder,
        source_runs=resolved.source_runs,
        build_bound=_BUILD_BOUND,
        clock=resolved.clock,
    )


def a_store_that_was_left_building(*, started_ago: timedelta) -> FakeDeckStore:
    """A deck whose build said it began and never reported back.

    What a server that was restarted mid-build leaves behind.
    """
    store = FakeDeckStore()
    store.put(
        Deck(
            slug="kundenfeedback",
            title="A talk",
            changed_at=_NOW - timedelta(minutes=2),
            owner_id=_OWNER,
            source_id=_SOURCE.id,
            commit=_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    store.put_attempt(
        "kundenfeedback",
        BuildAttempt(
            commit=_COMMIT,
            started_at=_NOW - started_ago,
            outcome=BuildOutcome.RUNNING,
            failure=None,
        ),
    )
    return store


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
    decks = decks_over(a_folder(name), fakes=DecksFakes(builder=builder))

    decks.refresh()

    assert decks.listed() == ()
    assert builder.built == []


def test_the_folder_name_stays_the_address_when_the_title_changes() -> None:
    mirror = carrying(a_folder("knowledge-fabric", title="Fabric"))
    decks = decks_over(mirror=mirror)
    first = refreshed(decks)

    mirror.carried[_SOURCE.id] = (a_folder("knowledge-fabric", title="Fabric v2"),)
    renamed = refreshed(decks)

    assert [deck.slug for deck in first] == ["knowledge-fabric"]
    assert [(deck.slug, deck.title) for deck in renamed] == [
        ("knowledge-fabric", "Fabric v2"),
    ]


def test_a_deck_belongs_to_the_owner_of_the_source_it_came_from() -> None:
    store = FakeDeckStore()

    decks_over(a_folder("kundenfeedback"), fakes=DecksFakes(store=store)).refresh()

    assert [deck.owner_id for deck in store.all()] == [_OWNER]


def test_without_a_configured_source_there_is_no_list_and_no_address() -> None:
    decks = decks_over(a_folder("kundenfeedback"), sources=having())

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
    decks = decks_over(a_folder("kundenfeedback"), fakes=DecksFakes(clock=clock))

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
    decks = decks_over(a_folder("kundenfeedback"), fakes=DecksFakes(builder=builder))

    decks.refresh()
    decks.refresh()

    assert builder.built == ["kundenfeedback"]


def test_a_push_builds_the_deck_it_changed_and_leaves_the_others_alone() -> None:
    builder = FakeBuildRunner()
    mirror = carrying(a_folder("kundenfeedback"), a_folder("knowledge-fabric"))
    decks = decks_over(mirror=mirror, fakes=DecksFakes(builder=builder))
    decks.refresh()

    mirror.carried[_SOURCE.id] = (
        a_folder("kundenfeedback", commit=_A_LATER_COMMIT),
        a_folder("knowledge-fabric"),
    )
    decks.refresh()

    assert builder.built == ["kundenfeedback", "knowledge-fabric", "kundenfeedback"]


def test_a_build_that_failed_leaves_the_talk_that_stands_standing() -> None:
    builder = FakeBuildRunner()
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror, fakes=DecksFakes(builder=builder))
    decks.refresh()
    standing = decks.built_talk("kundenfeedback")
    exported = decks.exported_pdf("kundenfeedback")
    built_when = page_of(decks, "kundenfeedback").built_ago

    builder.fails = True
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback", commit=_A_LATER_COMMIT),)
    decks.refresh()

    assert decks.built_talk("kundenfeedback") == standing
    assert decks.exported_pdf("kundenfeedback") == exported
    standing_page = page_of(decks, "kundenfeedback")
    assert standing_page.built_ago == built_when
    assert standing_page.commit == _SHORT_COMMIT


def test_a_deck_nothing_could_build_delivers_nothing_and_says_so() -> None:
    decks = decks_over(
        a_folder("kundenfeedback"),
        fakes=DecksFakes(builder=FakeBuildRunner(fails=True)),
    )

    decks.refresh()

    assert page_of(decks, "kundenfeedback").built_ago is None
    assert decks.built_talk("kundenfeedback") is None
    assert decks.exported_pdf("kundenfeedback") is None


def test_a_build_that_wrote_outside_the_builds_root_becomes_no_address() -> None:
    builder = FakeBuildRunner(writes_outside_the_root=True)
    decks = decks_over(a_folder("kundenfeedback"), fakes=DecksFakes(builder=builder))

    decks.refresh()

    assert builder.built == ["kundenfeedback"]
    page = page_of(decks, "kundenfeedback")
    assert page.built_ago is None
    assert page.state is DeckState.FAILED
    assert decks.built_talk("kundenfeedback") is None
    assert decks.exported_pdf("kundenfeedback") is None


def test_a_deck_being_built_says_so_with_the_commit_and_how_long_it_has_run() -> None:
    clock = FrozenClock(instant=_NOW)
    builder = FakeBuildRunner()
    decks = decks_over(
        a_folder("kundenfeedback"),
        fakes=DecksFakes(builder=builder, clock=clock),
    )
    seen: list[DeckPage] = []
    builder.while_building = lambda deck: seen.append(
        _after_it_has_run(decks, deck.slug, clock, timedelta(seconds=40)),
    )

    decks.refresh()

    building = seen[0]
    assert building.state is DeckState.BUILDING
    assert building.attempt is not None
    assert building.attempt.commit == _SHORT_COMMIT
    assert building.attempt.ago == timedelta(seconds=40)
    assert building.attempt.failure is None


def _after_it_has_run(
    decks: Decks,
    slug: str,
    clock: FrozenClock,
    so_far: timedelta,
) -> DeckPage:
    """That deck's page as a person opens it while the build is under way."""
    clock.advance(by=so_far)
    return page_of(decks, slug)


def _listed_states(decks: Decks) -> list[DeckState]:
    return [deck.state for deck in decks.listed()]


def test_a_deck_being_built_is_building_in_the_list() -> None:
    builder = FakeBuildRunner()
    decks = decks_over(
        a_folder("kundenfeedback", changed_ago=timedelta(minutes=2)),
        a_folder("knowledge-fabric", changed_ago=timedelta(days=1)),
        fakes=DecksFakes(builder=builder),
    )
    seen: list[list[DeckState]] = []
    builder.while_building = lambda _: seen.append(_listed_states(decks))

    decks.refresh()

    assert seen[0] == [DeckState.BUILDING, DeckState.NEVER_BUILT]


def test_a_build_that_failed_names_its_commit_and_what_the_toolchain_said() -> None:
    builder = FakeBuildRunner(fails=True, says=_WHAT_THE_TOOLCHAIN_SAID)
    clock = FrozenClock(instant=_NOW)
    decks = decks_over(
        a_folder("kundenfeedback"),
        fakes=DecksFakes(builder=builder, clock=clock),
    )

    decks.refresh()
    clock.advance(by=timedelta(minutes=26))

    page = page_of(decks, "kundenfeedback")
    assert page.state is DeckState.FAILED
    assert page.attempt is not None
    assert page.attempt.commit == _SHORT_COMMIT
    assert page.attempt.ago == timedelta(minutes=26)
    assert page.attempt.failure == _WHAT_THE_TOOLCHAIN_SAID
    assert _listed_states(decks) == [DeckState.FAILED]


def test_a_failed_build_leaves_the_standing_talk_where_it_is_and_says_failed() -> None:
    builder = FakeBuildRunner()
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror, fakes=DecksFakes(builder=builder))
    decks.refresh()
    standing = decks.built_talk("kundenfeedback")

    builder.fails = True
    builder.says = _WHAT_THE_TOOLCHAIN_SAID
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback", commit=_A_LATER_COMMIT),)
    decks.refresh()

    page = page_of(decks, "kundenfeedback")
    assert page.state is DeckState.FAILED
    assert page.commit == _SHORT_COMMIT
    assert page.attempt is not None
    assert page.attempt.commit == _SHORT_LATER_COMMIT
    assert decks.built_talk("kundenfeedback") == standing


def test_a_commit_that_failed_is_not_built_again_until_the_folder_moves() -> None:
    builder = FakeBuildRunner(fails=True, says=_WHAT_THE_TOOLCHAIN_SAID)
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror, fakes=DecksFakes(builder=builder))

    decks.refresh()
    decks.refresh()
    tried_while_nothing_moved = list(builder.built)
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback", commit=_A_LATER_COMMIT),)
    decks.refresh()

    assert tried_while_nothing_moved == ["kundenfeedback"]
    assert builder.built == ["kundenfeedback", "kundenfeedback"]


def test_a_build_that_works_again_leaves_no_failure_behind() -> None:
    builder = FakeBuildRunner(fails=True, says=_WHAT_THE_TOOLCHAIN_SAID)
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror, fakes=DecksFakes(builder=builder))
    decks.refresh()

    builder.fails = False
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback", commit=_A_LATER_COMMIT),)
    decks.refresh()

    page = page_of(decks, "kundenfeedback")
    assert page.state is DeckState.READY
    assert page.attempt is None
    assert _listed_states(decks) == [DeckState.READY]


def test_a_revert_to_the_talk_that_stands_is_ready_again_without_building() -> None:
    builder = FakeBuildRunner()
    clock = FrozenClock(instant=_NOW)
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(
        mirror=mirror,
        fakes=DecksFakes(builder=builder, clock=clock),
    )
    decks.refresh()
    standing = decks.built_talk("kundenfeedback")

    clock.advance(by=timedelta(minutes=10))
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback", commit=_A_LATER_COMMIT),)
    builder.fails = True
    builder.says = _WHAT_THE_TOOLCHAIN_SAID
    decks.refresh()
    while_the_push_was_broken = page_of(decks, "kundenfeedback")
    tried_before_the_revert = list(builder.built)

    clock.advance(by=timedelta(minutes=5))
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback"),)
    decks.refresh()

    reverted = page_of(decks, "kundenfeedback")
    assert while_the_push_was_broken.state is DeckState.FAILED
    assert reverted.state is DeckState.READY
    assert reverted.attempt is None
    assert reverted.commit == _SHORT_COMMIT
    # The talk that stands is the one that was built before the broken push:
    # its own build time, and no second run of the toolchain.
    assert reverted.built_ago == timedelta(minutes=15)
    assert decks.built_talk("kundenfeedback") == standing
    assert builder.built == tried_before_the_revert
    assert _listed_states(decks) == [DeckState.READY]


def test_a_build_that_died_with_the_server_is_failed_at_the_next_refresh() -> None:
    builder = FakeBuildRunner()
    decks = decks_over(
        a_folder("kundenfeedback"),
        fakes=DecksFakes(
            store=a_store_that_was_left_building(started_ago=_BUILD_BOUND * 2),
            builder=builder,
        ),
    )

    decks.refresh()

    page = page_of(decks, "kundenfeedback")
    assert page.state is DeckState.FAILED
    assert page.attempt is not None
    assert page.attempt.failure is None
    assert builder.built == []


def test_a_build_still_inside_its_bound_keeps_saying_it_is_building() -> None:
    builder = FakeBuildRunner()
    decks = decks_over(
        a_folder("kundenfeedback"),
        fakes=DecksFakes(
            store=a_store_that_was_left_building(started_ago=_BUILD_BOUND / 2),
            builder=builder,
        ),
    )

    decks.refresh()

    assert page_of(decks, "kundenfeedback").state is DeckState.BUILDING
    assert builder.built == []


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
    decks_over(a_folder("kundenfeedback"), fakes=DecksFakes(store=store)).refresh()

    page = page_of(
        decks_over(sources=having(), fakes=DecksFakes(store=store)),
        "kundenfeedback",
    )

    assert page.source is None
    assert page.commit == _SHORT_COMMIT


def test_the_list_shows_the_last_refresh_and_reads_no_source() -> None:
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror)

    before_any_refresh = decks.listed()
    decks.refresh()
    mirror.carried[_SOURCE.id] = (a_folder("pushed-after-the-refresh"),)

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
    mirror = carrying(a_folder("alter-vortrag"), a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror)
    refreshed(decks)

    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback"),)
    after_the_delete = refreshed(decks)

    assert [deck.slug for deck in after_the_delete] == ["kundenfeedback"]


def test_a_folder_pushed_again_is_the_same_deck_with_the_same_owner() -> None:
    mirror = carrying(a_folder("kundenfeedback", title="Feedback"))
    store = FakeDeckStore()
    decks = decks_over(mirror=mirror, fakes=DecksFakes(store=store))
    refreshed(decks)

    mirror.carried[_SOURCE.id] = ()
    while_it_was_gone = refreshed(decks)
    mirror.carried[_SOURCE.id] = (a_folder("kundenfeedback", title="Feedback"),)
    after_it_came_back = refreshed(decks)

    assert while_it_was_gone == ()
    assert [deck.slug for deck in after_it_came_back] == ["kundenfeedback"]
    assert [deck.owner_id for deck in store.all()] == [_OWNER]


def test_a_source_that_cannot_be_read_leaves_every_deck_listed() -> None:
    mirror = carrying(a_folder("kundenfeedback"))
    decks = decks_over(mirror=mirror)
    refreshed(decks)

    mirror.carried[_SOURCE.id] = None
    while_the_source_was_unreadable = refreshed(decks)

    assert [deck.slug for deck in while_the_source_was_unreadable] == ["kundenfeedback"]


def test_the_source_the_seed_writes_is_taken_in_by_the_refresh_that_seeded_it() -> None:
    decks = decks_over(
        sources=having(seeds=_SOURCE),
        mirror=carrying(a_folder("kundenfeedback")),
    )

    listed = refreshed(decks)

    assert [deck.slug for deck in listed] == ["kundenfeedback"]


def test_two_sources_each_list_their_own_decks() -> None:
    store = FakeDeckStore()
    decks = decks_over(
        sources=having(_SOURCE, _ANOTHER_SOURCE),
        mirror=FakeDeckFolders(
            carried={
                _SOURCE.id: (a_folder("kundenfeedback"),),
                _ANOTHER_SOURCE.id: (a_folder("knowledge-fabric"),),
            },
        ),
        fakes=DecksFakes(store=store),
    )

    listed = refreshed(decks)

    assert sorted(deck.slug for deck in listed) == [
        "knowledge-fabric",
        "kundenfeedback",
    ]
    assert {deck.slug: deck.source_id for deck in store.all()} == {
        "kundenfeedback": _SOURCE.id,
        "knowledge-fabric": _ANOTHER_SOURCE.id,
    }


def test_a_folder_deleted_in_one_source_leaves_the_other_sources_decks_listed() -> None:
    mirror = FakeDeckFolders(
        carried={
            _SOURCE.id: (a_folder("kundenfeedback"),),
            _ANOTHER_SOURCE.id: (a_folder("knowledge-fabric"),),
        },
    )
    decks = decks_over(sources=having(_SOURCE, _ANOTHER_SOURCE), mirror=mirror)
    refreshed(decks)

    mirror.carried[_SOURCE.id] = ()
    after_the_delete = refreshed(decks)

    assert [deck.slug for deck in after_the_delete] == ["knowledge-fabric"]


def test_a_folder_name_another_source_carries_stays_with_the_source_that_had_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeDeckStore()
    decks = decks_over(
        sources=having(_SOURCE, _ANOTHER_SOURCE),
        mirror=FakeDeckFolders(
            carried={
                _SOURCE.id: (a_folder("kundenfeedback", title="The first"),),
                _ANOTHER_SOURCE.id: (a_folder("kundenfeedback", title="The second"),),
            },
        ),
        fakes=DecksFakes(store=store),
    )

    with caplog.at_level(logging.WARNING):
        listed = refreshed(decks)

    assert [(deck.slug, deck.title) for deck in listed] == [
        ("kundenfeedback", "The first"),
    ]
    assert [deck.source_id for deck in store.all()] == [_SOURCE.id]
    assert "kundenfeedback" in caplog.text
    assert _ANOTHER_SOURCE.name in caplog.text


def test_a_deck_is_built_out_of_the_source_that_carried_it() -> None:
    builder = FakeBuildRunner()
    decks = decks_over(
        sources=having(_SOURCE, _ANOTHER_SOURCE),
        mirror=FakeDeckFolders(
            carried={
                _SOURCE.id: (a_folder("kundenfeedback"),),
                _ANOTHER_SOURCE.id: (a_folder("knowledge-fabric"),),
            },
        ),
        fakes=DecksFakes(builder=builder),
    )

    decks.refresh()

    assert builder.from_source == {
        "kundenfeedback": _SOURCE.id,
        "knowledge-fabric": _ANOTHER_SOURCE.id,
    }


def test_a_deck_page_names_the_address_of_the_source_that_carried_it() -> None:
    decks = decks_over(
        sources=having(_SOURCE, _ANOTHER_SOURCE),
        mirror=FakeDeckFolders(
            carried={
                _SOURCE.id: (a_folder("kundenfeedback"),),
                _ANOTHER_SOURCE.id: (a_folder("knowledge-fabric"),),
            },
        ),
    )
    decks.refresh()

    assert page_of(decks, "kundenfeedback").source == _SOURCE.url
    assert page_of(decks, "knowledge-fabric").source == _ANOTHER_SOURCE.url


def test_an_empty_list_names_no_address_while_two_sources_could_carry_a_deck() -> None:
    decks = decks_over(sources=having(_SOURCE, _ANOTHER_SOURCE))

    assert refreshed(decks) == ()
    assert decks.source_address() is None


def test_a_source_that_cannot_be_read_stops_no_other_sources_refresh() -> None:
    mirror = FakeDeckFolders(
        carried={
            _SOURCE.id: (a_folder("kundenfeedback"),),
            _ANOTHER_SOURCE.id: (a_folder("knowledge-fabric"),),
        },
    )
    decks = decks_over(sources=having(_SOURCE, _ANOTHER_SOURCE), mirror=mirror)
    refreshed(decks)

    mirror.carried[_SOURCE.id] = None
    mirror.carried[_ANOTHER_SOURCE.id] = (
        a_folder("knowledge-fabric"),
        a_folder("pushed-while-the-other-was-unreadable"),
    )
    while_one_source_was_unreadable = refreshed(decks)

    assert sorted(deck.slug for deck in while_one_source_was_unreadable) == [
        "knowledge-fabric",
        "kundenfeedback",
        "pushed-while-the-other-was-unreadable",
    ]


def test_a_poll_that_reaches_a_source_records_a_run_carrying_its_commit() -> None:
    run_store = FakeSourceRunStore()
    mirror = FakeDeckFolders(
        carried={_SOURCE.id: (a_folder("kundenfeedback"),)},
        commits={_SOURCE.id: _COMMIT},
    )
    decks = decks_over(mirror=mirror, fakes=DecksFakes(source_runs=run_store))

    decks.refresh()

    run = run_store.newest(_SOURCE.id)
    assert run is not None
    assert run.source_id == _SOURCE.id
    assert run.at == _NOW
    assert run.outcome is SourceRunOutcome.SUCCESS
    assert run.commit == _COMMIT
    assert run.reason is None


def test_an_unreachable_source_records_a_failed_run_naming_why() -> None:
    run_store = FakeSourceRunStore()
    mirror = FakeDeckFolders(
        carried={_SOURCE.id: None},
        failures={_SOURCE.id: SourceRunFailure.CREDENTIAL_UNRESOLVABLE},
    )
    decks = decks_over(mirror=mirror, fakes=DecksFakes(source_runs=run_store))

    decks.refresh()

    run = run_store.newest(_SOURCE.id)
    assert run is not None
    assert run.at == _NOW
    assert run.outcome is SourceRunOutcome.FAILURE
    assert run.commit is None
    assert run.reason is SourceRunFailure.CREDENTIAL_UNRESOLVABLE


def test_a_source_nobody_has_polled_lists_as_never_fetched() -> None:
    assert decks_over().listed_sources() == (
        ListedSource(
            name=_SOURCE.name,
            url=_SOURCE.url,
            access=access_kind_of(_SOURCE.url),
            state=SourceState.NEVER_FETCHED,
            age=None,
        ),
    )


def test_a_successful_newest_run_lists_as_reachable_with_that_runs_age() -> None:
    runs = FakeSourceRunStore()
    runs.record(
        SourceRun(
            source_id=_SOURCE.id,
            at=_NOW - timedelta(minutes=3),
            outcome=SourceRunOutcome.SUCCESS,
            commit=_COMMIT,
            reason=None,
        ),
    )

    listed = decks_over(fakes=DecksFakes(source_runs=runs)).listed_sources()

    assert listed[0].state is SourceState.REACHABLE
    assert listed[0].age == timedelta(minutes=3)


def test_a_failed_newest_run_lists_as_error_even_when_the_reason_is_unresolvable() -> (
    None
):
    runs = FakeSourceRunStore()
    runs.record(
        SourceRun(
            source_id=_SOURCE.id,
            at=_NOW - timedelta(hours=2),
            outcome=SourceRunOutcome.FAILURE,
            commit=None,
            reason=SourceRunFailure.CREDENTIAL_UNRESOLVABLE,
        ),
    )

    listed = decks_over(fakes=DecksFakes(source_runs=runs)).listed_sources()

    assert listed[0].state is SourceState.ERROR
    assert listed[0].age == timedelta(hours=2)


def test_a_refused_login_lists_as_refused_rather_than_error() -> None:
    runs = FakeSourceRunStore()
    runs.record(
        SourceRun(
            source_id=_SOURCE.id,
            at=_NOW - timedelta(minutes=5),
            outcome=SourceRunOutcome.FAILURE,
            commit=None,
            reason=SourceRunFailure.REFUSED,
        ),
    )

    listed = decks_over(fakes=DecksFakes(source_runs=runs)).listed_sources()

    assert listed[0].state is SourceState.REFUSED
    assert listed[0].age == timedelta(minutes=5)


def test_the_list_reads_the_newest_run_only() -> None:
    runs = FakeSourceRunStore()
    runs.record(
        SourceRun(
            source_id=_SOURCE.id,
            at=_NOW - timedelta(hours=1),
            outcome=SourceRunOutcome.SUCCESS,
            commit=_COMMIT,
            reason=None,
        ),
    )
    runs.record(
        SourceRun(
            source_id=_SOURCE.id,
            at=_NOW - timedelta(minutes=2),
            outcome=SourceRunOutcome.FAILURE,
            commit=None,
            reason=SourceRunFailure.UNREACHABLE,
        ),
    )

    listed = decks_over(fakes=DecksFakes(source_runs=runs)).listed_sources()

    assert listed[0].state is SourceState.ERROR
    assert listed[0].age == timedelta(minutes=2)


def test_refreshing_one_source_leaves_another_unpolled() -> None:
    run_store = FakeSourceRunStore()
    decks = decks_over(
        sources=having(_SOURCE, _ANOTHER_SOURCE),
        mirror=FakeDeckFolders(
            carried={
                _SOURCE.id: (a_folder("kundenfeedback"),),
                _ANOTHER_SOURCE.id: (a_folder("knowledge-fabric"),),
            },
        ),
        fakes=DecksFakes(source_runs=run_store),
    )

    assert decks.refresh_named(_SOURCE.name)
    assert run_store.newest(_SOURCE.id) is not None
    assert run_store.newest(_ANOTHER_SOURCE.id) is None


def test_refreshing_a_name_this_instance_does_not_have_does_nothing() -> None:
    run_store = FakeSourceRunStore()
    decks = decks_over(fakes=DecksFakes(source_runs=run_store))

    assert not decks.refresh_named("no-such-source")
    assert run_store.newest(_SOURCE.id) is None


def test_each_sources_run_is_recorded_under_its_own_id() -> None:
    run_store = FakeSourceRunStore()
    mirror = FakeDeckFolders(
        carried={
            _SOURCE.id: (a_folder("kundenfeedback"),),
            _ANOTHER_SOURCE.id: None,
        },
    )
    decks = decks_over(
        sources=having(_SOURCE, _ANOTHER_SOURCE),
        mirror=mirror,
        fakes=DecksFakes(source_runs=run_store),
    )

    decks.refresh()

    first_run = run_store.newest(_SOURCE.id)
    second_run = run_store.newest(_ANOTHER_SOURCE.id)
    assert first_run is not None
    assert second_run is not None
    assert first_run.outcome is SourceRunOutcome.SUCCESS
    assert second_run.outcome is SourceRunOutcome.FAILURE


_READ_ONLY = "a-read-only-token"
_HTTPS_URL = "https://git.example.invalid/talks.git"
_MINIMUM_WEBHOOK_SECRET_LENGTH = 32


def _add(
    decks: Decks,
    *,
    name: str = "talks",
    url: str = _HTTPS_URL,
    access: str = "https",
    secret: str = _READ_ONLY,
) -> AddedSource | SourceRefusal:
    return decks.add_source(
        name=name,
        url=url,
        access=access,
        secret=secret,
        owner_id=_OWNER,
    )


def test_adding_a_source_stores_it_fetches_it_and_returns_a_webhook_secret() -> None:
    store = having()
    run_store = FakeSourceRunStore()
    decks = decks_over(
        sources=store,
        fakes=DecksFakes(source_runs=run_store),
    )

    added = _add(decks)

    assert isinstance(added, AddedSource)
    assert added.source.name == "talks"
    assert added.source.url == _HTTPS_URL
    assert added.source.ref == "main"
    assert added.source.secret_location is SecretLocation.STORED
    assert added.source.owner_id == _OWNER
    assert len(added.webhook_secret) >= _MINIMUM_WEBHOOK_SECRET_LENGTH
    assert set(added.webhook_secret) <= set(string.ascii_letters + string.digits + "-_")
    assert added.webhook_secret != _READ_ONLY
    stored_hash = store.hook_secret_hash("talks")
    assert stored_hash is not None
    assert (
        stored_hash.hex() == hashlib.sha256(added.webhook_secret.encode()).hexdigest()
    )
    assert run_store.newest(added.source.id) is not None


@pytest.mark.parametrize(
    ("name", "url", "access", "secret", "reason"),
    [
        pytest.param(
            "Talks",
            _HTTPS_URL,
            "https",
            _READ_ONLY,
            SourceRefusal.MALFORMED_NAME,
            id="uppercase",
        ),
        pytest.param(
            "new",
            _HTTPS_URL,
            "https",
            _READ_ONLY,
            SourceRefusal.MALFORMED_NAME,
            id="the form's own path",
        ),
        pytest.param(
            "-talks",
            _HTTPS_URL,
            "https",
            _READ_ONLY,
            SourceRefusal.MALFORMED_NAME,
            id="leading hyphen",
        ),
        pytest.param(
            "t" * 65,
            _HTTPS_URL,
            "https",
            _READ_ONLY,
            SourceRefusal.MALFORMED_NAME,
            id="too long",
        ),
        pytest.param(
            "talks",
            _HTTPS_URL,
            "https",
            "",
            SourceRefusal.BLANK_ACCESS,
            id="blank secret",
        ),
        pytest.param(
            "talks",
            "https://user:token@git.example.invalid/talks.git",
            "https",
            _READ_ONLY,
            SourceRefusal.USERINFO,
            id="password in the URL",
        ),
        pytest.param(
            "talks",
            "git@git.example.invalid:talks.git",
            "https",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="ssh URL with https radio",
        ),
        pytest.param(
            "talks",
            _HTTPS_URL,
            "ssh",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="ssh radio",
        ),
        pytest.param(
            "talks",
            "file:///tmp/talks.git",
            "https",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="file URL",
        ),
        pytest.param(
            "talks",
            "http://git.example.invalid/talks.git",
            "https",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="http URL",
        ),
        pytest.param(
            "talks",
            "https://git.example.invalid/talks.git\x00evil",
            "https",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="nul in the URL",
        ),
        pytest.param(
            "talks",
            "https://git.example.invalid/talks.git\nevil",
            "https",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="newline in the URL",
        ),
        pytest.param(
            "talks",
            "https://git.example.invalid/talks.git\tevil",
            "https",
            _READ_ONLY,
            SourceRefusal.ACCESS_MISMATCH,
            id="tab in the URL",
        ),
    ],
)
def test_a_draft_that_cannot_be_a_source_is_refused(
    name: str,
    url: str,
    access: str,
    secret: str,
    reason: SourceRefusal,
) -> None:
    store = having()

    refused = _add(
        decks_over(sources=store),
        name=name,
        url=url,
        access=access,
        secret=secret,
    )
    assert refused is reason
    assert store.all() == ()


def test_refusing_a_control_character_url_leaves_other_sources_to_poll() -> None:
    store = having()
    run_store = FakeSourceRunStore()
    decks = decks_over(sources=store, fakes=DecksFakes(source_runs=run_store))
    added = _add(decks)
    assert isinstance(added, AddedSource)

    refused = _add(
        decks,
        name="evil",
        url="https://git.example.invalid/talks.git\x00evil",
    )

    assert refused is SourceRefusal.ACCESS_MISMATCH
    assert store.all() == (added.source,)

    decks.refresh()

    run = run_store.newest(added.source.id)
    assert run is not None
    assert run.outcome is SourceRunOutcome.SUCCESS
    assert [source.name for source in decks.listed_sources()] == ["talks"]


def test_a_duplicate_name_or_url_is_refused() -> None:
    first = having()
    decks = decks_over(sources=first)
    assert isinstance(_add(decks), AddedSource)

    assert (
        _add(decks, url="https://git.example.invalid/other.git")
        is SourceRefusal.DUPLICATE_NAME
    )
    assert _add(decks, name="other") is SourceRefusal.DUPLICATE_URL


def test_adding_a_source_does_not_rewrite_a_seeded_environment_source() -> None:
    seeded = replace(
        a_source("decks", identifier="the-configured-source"),
        secret_location=SecretLocation.ENVIRONMENT,
    )
    store = having(seeded)

    added = _add(decks_over(sources=store))

    assert isinstance(added, AddedSource)
    kept = next(source for source in store.all() if source.id == seeded.id)
    assert kept.secret_location is SecretLocation.ENVIRONMENT


def test_a_hook_call_with_the_stored_secret_refreshes_that_source() -> None:
    store = having()
    run_store = FakeSourceRunStore()
    decks = decks_over(sources=store, fakes=DecksFakes(source_runs=run_store))
    added = _add(decks)
    assert isinstance(added, AddedSource)

    assert decks.accept_hook(added.source.name, added.webhook_secret)
    assert run_store.newest(added.source.id) is not None


def test_an_insert_that_lost_the_race_is_a_duplicate_name() -> None:
    class StoreThatRejectsWrites(FakeSourceStore):
        def add(self, write: SourceWrite) -> Source | None:
            del write
            return None

    assert _add(decks_over(sources=StoreThatRejectsWrites())) is (
        SourceRefusal.DUPLICATE_NAME
    )


def test_a_hook_call_with_the_wrong_secret_or_name_is_refused() -> None:
    store = having()
    decks = decks_over(sources=store)
    added = _add(decks)
    assert isinstance(added, AddedSource)

    assert not decks.accept_hook(added.source.name, "guessed")
    assert not decks.accept_hook("no-such-source", added.webhook_secret)
    assert not decks.accept_hook(added.source.name, "")


def test_https_and_ssh_urls_name_their_access_kind() -> None:
    https = "https://git.example.invalid/talks.git"
    http = "http://git.example.invalid/talks.git"
    ssh = "ssh://git@git.example.invalid/talks.git"
    scp = "git@git.example.invalid:talks.git"
    assert access_kind_of(https) is AccessKind.HTTPS
    assert access_kind_of(http) is None
    assert access_kind_of(ssh) is AccessKind.SSH
    assert access_kind_of(scp) is AccessKind.SSH
    assert access_kind_of("file:///tmp/talks.git") is None
    assert access_kind_of("https://git.example.invalid/talks.git\x00evil") is None
