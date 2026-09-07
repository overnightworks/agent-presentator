"""Taking decks in from a source, building them, and what the lobby then shows.

Which folder counts as a deck, which deck is out of date, when a build may
become the talk that is delivered, and what a deck page says is decided here;
git, TOML, SQL, and the build toolchain stay outside (ADR 0001, ADR 0005).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Final

from presentator.contracts.decks import (
    SLIDES_FILE,
    Artefacts,
    Build,
    BuildAttempt,
    BuildOutcome,
    Deck,
    DeckPage,
    DeckState,
    ListedDeck,
    ListedSource,
    ShownAttempt,
    Source,
    SourceRun,
    SourceRunOutcome,
    SourceState,
)
from presentator.ports.clock import Clock
from presentator.ports.decks import (
    BuildRunner,
    DeckFolders,
    DeckStore,
    SourceRuns,
    SourceStore,
)

_log = logging.getLogger(__name__)

# A person compares the commit on the page with the one their push wrote, and
# reads it off the screen; the first characters are what git itself shows.
_SHORT_COMMIT: Final = 7
# A folder's name is one path element: it stays one word of a header value and
# never reaches past the directory it is joined under.
_NAMES_NO_FOLDER: Final = frozenset({"", ".", ".."})
_NEVER_IN_A_FOLDER_NAME: Final = frozenset('/\\"')
_NOT_A_FOLDERS_OWN_NAME: Final = "folder %r is not a folder's own name, skipped"
_NAME_BELONGS_TO_ANOTHER_SOURCE: Final = (
    "folder %r is already carried by another source, skipped for source %s"
)


def _is_a_plain_folder_name(candidate: str) -> bool:
    """Whether the candidate is a folder's own name and nothing besides."""
    return (
        candidate not in _NAMES_NO_FOLDER
        and candidate.isprintable()
        and not _NEVER_IN_A_FOLDER_NAME.intersection(candidate)
    )


def _state_of(deck: Deck) -> DeckState:
    """The one word for what this deck is, read off its talk and its attempt.

    An attempt outranks the talk that stands, because it is the newer news: a
    deck whose last build failed is failed even while yesterday's talk still
    opens (line 16).
    """
    attempt = deck.attempt
    if attempt is None:
        return DeckState.READY if deck.build is not None else DeckState.NEVER_BUILT
    if attempt.outcome is BuildOutcome.RUNNING:
        return DeckState.BUILDING
    return DeckState.FAILED


@dataclass(frozen=True, slots=True, kw_only=True)
class Decks:
    """Every use case the lobby has around the talks it knows."""

    sources: SourceStore
    folders: DeckFolders
    store: DeckStore
    builder: BuildRunner
    source_runs: SourceRuns
    clock: Clock
    # How long a build may take before the refresh stops believing it is still
    # running: a process that died with the server leaves its attempt behind,
    # and nobody may read that as building for ever.
    build_bound: timedelta
    _one_at_a_time: Lock = field(default_factory=Lock)

    def refresh(self) -> None:
        """Take the sources in and build what changed, one refresh at a time.

        Whoever noticed the push calls this. A further request while one runs is
        already served by it, so a flood of them pulls once rather than once
        each, and the builds of one refresh follow one another instead of
        competing for the machine.
        """
        self._run_one_at_a_time(self._take_in_and_build)

    def refresh_named(self, name: str) -> bool:
        """Take that one source in and build what changed, if this instance has it.

        A name nobody stored is not a source to fetch; the caller says so rather
        than walking every source and doing nothing. A refresh already running
        is already serving the same lock, so this call returns as if it ran.
        """
        source = next(
            (item for item in self.sources.all() if item.name == name),
            None,
        )
        if source is None:
            return False
        self._run_one_at_a_time(partial(self._take_in_and_build_one, source))
        return True

    def listed_sources(self) -> tuple[ListedSource, ...]:
        """Each source as the list shows it, reading the newest run only.

        No run is never-fetched; a successful newest run is reachable; a failed
        one is error, including a credential the instance could not resolve.
        """
        now = self.clock.now()
        return tuple(self._listed_source(source, now) for source in self.sources.all())

    def listed(self) -> tuple[ListedDeck, ...]:
        """The stored decks, newest changed first, reading no source.

        A page view costs a read, never a pull: what the source carries is the
        refresh's business.
        """
        now = self.clock.now()
        newest_first = sorted(
            self.store.all(),
            key=lambda deck: deck.changed_at,
            reverse=True,
        )
        return tuple(
            ListedDeck(
                slug=deck.slug,
                title=deck.title,
                state=_state_of(deck),
                age=now - deck.changed_at,
            )
            for deck in newest_first
        )

    def page(self, slug: str) -> DeckPage | None:
        """What that deck's page says, or nothing while no deck carries the slug.

        The commit named is the one the delivered talk was built from, not the
        one the source carries now: a person reads it to see whether their push
        is in what is on the screen (line 7), and while nothing is built there
        is nothing on the screen but what the source last carried.
        """
        deck = self.store.get(slug)
        if deck is None:
            return None
        built = deck.build
        return DeckPage(
            slug=deck.slug,
            title=deck.title,
            source=self._address_of(deck),
            commit=(deck.commit if built is None else built.commit)[:_SHORT_COMMIT],
            built_ago=None if built is None else self.clock.now() - built.built_at,
            state=_state_of(deck),
            attempt=self._shown(deck.attempt),
        )

    def _shown(self, attempt: BuildAttempt | None) -> ShownAttempt | None:
        """What the page says about the build that ran last, while one has run.

        The commit is shortened the way the delivered one is: a person reads
        both off the same screen to see which push is which.
        """
        if attempt is None:
            return None
        return ShownAttempt(
            commit=attempt.commit[:_SHORT_COMMIT],
            ago=self.clock.now() - attempt.started_at,
            failure=attempt.failure,
        )

    def _address_of(self, deck: Deck) -> str | None:
        """The address the source that carried this deck is mirrored from.

        A deck names the source it came from, never whichever source the
        instance happens to have first, so two sources cannot show one
        another's address on a page.
        """
        return next(
            (
                source.url
                for source in self.sources.all()
                if source.id == deck.source_id
            ),
            None,
        )

    def built_talk(self, slug: str) -> Path | None:
        """The directory the deck's talk is delivered from, while one is built."""
        deck = self.store.get(slug)
        if deck is None or deck.build is None:
            return None
        return deck.build.directory

    def exported_pdf(self, slug: str) -> Path | None:
        """The file the deck's PDF is handed over as, while one is exported.

        A slug that is not a folder's plain name names no deck here: the export
        is offered under that name, so a separator, a quote, or a line break in
        it would otherwise reach the answer's own header.
        """
        if not _is_a_plain_folder_name(slug):
            return None
        deck = self.store.get(slug)
        if deck is None or deck.build is None:
            return None
        return deck.build.pdf

    def source_address(self) -> str | None:
        """The git address an empty list names, while one source is all there is.

        With a second source there is no single address the list could name,
        so it says only that it is empty.
        """
        sources = self.sources.all()
        return sources[0].url if len(sources) == 1 else None

    def _listed_source(self, source: Source, now: datetime) -> ListedSource:
        """The row for that source, derived from its newest run and nothing else."""
        run = self.source_runs.newest(source.id)
        if run is None:
            return ListedSource(
                name=source.name,
                url=source.url,
                state=SourceState.NEVER_FETCHED,
                age=None,
            )
        return ListedSource(
            name=source.name,
            url=source.url,
            state=(
                SourceState.REACHABLE
                if run.outcome is SourceRunOutcome.SUCCESS
                else SourceState.ERROR
            ),
            age=now - run.at,
        )

    def _run_one_at_a_time(self, work: Callable[[], None]) -> None:
        """Run that work, or drop it while another refresh already holds the lock."""
        if not self._one_at_a_time.acquire(blocking=False):
            return
        try:
            work()
        finally:
            self._one_at_a_time.release()

    def _take_in_and_build(self) -> None:
        """Make the configured source a row, then walk the sources one by one.

        A source is taken in and built before the next one is read, so a
        refresh costs its sources' pull bounds one after another instead of
        opening as many pulls at once as the instance has sources.
        """
        self.sources.seed()
        for source in self.sources.all():
            self._take_in_and_build_one(source)

    def _take_in_and_build_one(self, source: Source) -> None:
        """Take that source in and build the decks of it that changed."""
        self._take_in(source)
        self._build_what_changed(source)

    def _take_in(self, source: Source) -> None:
        """Store every folder that carries both a manifest and slides.

        The folder name becomes the slug, so a changed title reaches no address.
        A folder whose name is not a folder's own name — such as `..`, planted
        as a git tree entry — is skipped here rather than stored, so it is never
        used as a path component by this deck or its build. A folder the source
        has stopped carrying is marked as removed rather than deleted, and a
        folder that comes back under its old name loses that mark, so it is the
        deck it was. A source nobody could read carries no such news, and leaves
        every deck where it is. A folder whose name another source already
        carries is skipped too: the slug is the address of one deck on this
        instance, and the source that carried it first keeps it. Whatever the
        poll found or failed to is recorded as this source's newest run, so a
        board can show that the mechanism ran and what it saw.
        """
        at = self.clock.now()
        poll = self.folders.folders(source)
        self.source_runs.record(
            SourceRun(
                source_id=source.id,
                at=at,
                outcome=(
                    SourceRunOutcome.FAILURE
                    if poll.folders is None
                    else SourceRunOutcome.SUCCESS
                ),
                commit=poll.commit,
                reason=poll.failure,
            ),
        )
        if poll.folders is None:
            return
        for folder in poll.folders:
            if not _is_a_plain_folder_name(folder.name):
                _log.warning(_NOT_A_FOLDERS_OWN_NAME, folder.name)
                continue
            if folder.title is None or SLIDES_FILE not in folder.file_names:
                continue
            taken_in = self.store.put(
                Deck(
                    slug=folder.name,
                    title=folder.title,
                    changed_at=folder.changed_at,
                    owner_id=source.owner_id,
                    source_id=source.id,
                    commit=folder.commit,
                    build=None,
                    attempt=None,
                ),
            )
            if not taken_in:
                _log.warning(_NAME_BELONGS_TO_ANOTHER_SOURCE, folder.name, source.name)
        self.store.mark_removed_except(
            present=frozenset(folder.name for folder in poll.folders),
            source_id=source.id,
            at=at,
        )

    def _build_what_changed(self, source: Source) -> None:
        """Build that source's decks whose commit nothing has tried yet.

        The commit is the whole change check: a push moves one folder's commit,
        so only that deck is built again and the other talks are left alone. A
        folder standing at the commit its talk was built from needs no build at
        all, and a commit that was already tried is not tried again, so a deck
        that cannot build costs one build and not one per refresh until it is
        pushed again. A deck is built out of the mirror of its own source, so a
        source's walk passes over the decks another source carried.
        """
        for deck in self.store.all():
            if deck.source_id != source.id:
                continue
            self._give_up_on_a_build_that_never_returned(deck)
            standing = deck.build
            if standing is not None and standing.commit == deck.commit:
                self._deliver_the_standing_talk_again(deck, standing)
                continue
            if self._was_already_tried(deck):
                continue
            self._switch_over(deck, source)

    def _deliver_the_standing_talk_again(self, deck: Deck, standing: Build) -> None:
        """Make the talk that stands this commit's talk again, building nothing.

        Reverting a broken push is how a person undoes it in git (line 16): the
        folder is back at the commit the standing talk was built from, so that
        talk is current again and the failure recorded against the push that
        was reverted goes with the one statement a successful switch writes.
        Its build time does not move, because that build is the one that ran.
        A deck that carries no attempt is left alone, so an unchanged deck
        costs a refresh no write at all.
        """
        if deck.attempt is None:
            return
        self.store.put_build(deck.slug, standing)

    def _was_already_tried(self, deck: Deck) -> bool:
        """Whether a build was already started for the commit the folder carries."""
        return deck.attempt is not None and deck.attempt.commit == deck.commit

    def _give_up_on_a_build_that_never_returned(self, deck: Deck) -> None:
        """Call an attempt that outlived the build bound failed, not running.

        Builds follow one another inside one refresh, so an attempt still
        saying it runs when the next refresh reads it belongs to a process that
        is gone — the server was restarted while it built. Nobody may read that
        as a deck building for ever, and it left no words to show. The bound is
        one toolchain step's, not the whole walk's, on purpose: no live build
        is ever read here, so the bound only has to be too long for a leftover
        to be mistaken for one that just began.
        """
        attempt = deck.attempt
        if attempt is None or attempt.outcome is not BuildOutcome.RUNNING:
            return
        if self.clock.now() - attempt.started_at <= self.build_bound:
            return
        self.store.put_attempt(
            deck.slug,
            replace(attempt, outcome=BuildOutcome.FAILED),
        )

    def _switch_over(self, deck: Deck, source: Source) -> None:
        """Deliver this deck from its new build, once there really is one.

        The attempt is recorded before the toolchain starts, so a page opened
        while it runs says so, and a run that dies with the server leaves a
        record behind rather than nothing. A build that failed, and one that
        left artefacts anywhere but under the root they belong in, write no
        pointer at all: whatever the deck was delivering before keeps being
        delivered, and the attempt says why it is still that one.
        """
        attempt = BuildAttempt(
            commit=deck.commit,
            started_at=self.clock.now(),
            outcome=BuildOutcome.RUNNING,
            failure=None,
        )
        self.store.put_attempt(deck.slug, attempt)
        built = self.builder.build(deck, source=source)
        if not isinstance(built, Artefacts):
            self._failed(deck, attempt, said=built.text)
            return
        if not self.builder.holds(built):
            self._failed(deck, attempt, said=None)
            return
        self.store.put_build(
            deck.slug,
            Build(
                directory=built.directory,
                pdf=built.pdf,
                commit=deck.commit,
                built_at=self.clock.now(),
            ),
        )

    def _failed(self, deck: Deck, attempt: BuildAttempt, *, said: str | None) -> None:
        """Close this attempt as failed, keeping the moment it started."""
        self.store.put_attempt(
            deck.slug,
            replace(attempt, outcome=BuildOutcome.FAILED, failure=said),
        )
