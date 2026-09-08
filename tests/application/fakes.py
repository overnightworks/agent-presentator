"""In-memory stands-in for the ports, so the use cases run pure."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

from presentator.application.identity import IDLE_WINDOW
from presentator.contracts.decks import (
    RECENT_SOURCE_RUNS,
    Artefacts,
    Build,
    BuildAttempt,
    BuildFailure,
    ConnectionCheckResult,
    Deck,
    DeckFolder,
    SecretLocation,
    Source,
    SourcePoll,
    SourceRun,
    SourceRunFailure,
    SourceWrite,
)
from presentator.contracts.models import (
    Account,
    FirstStartClosedError,
    Role,
    Session,
    User,
)
from presentator.contracts.preferences import InstanceSettings, PersonPreferences
from presentator.contracts.text import LobbyText

# How long one thread waits for another before a test calls the run stuck.
PATIENCE: Final = timedelta(seconds=5)
# What a poll that does not name its own commit or failure carries instead:
# most tests only care about the folders a source carries, never these two.
_A_FETCHED_COMMIT: Final = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_AN_UNNAMED_FAILURE: Final = SourceRunFailure.UNREACHABLE
# What Check connection finds when no test says otherwise: the form's own
# happy path, so a test unrelated to the probe itself never has to arrange it.
A_REACHABLE_CHECK: Final = ConnectionCheckResult(
    failure=None,
    commit=_A_FETCHED_COMMIT[:7],
    detail=None,
)


@dataclass
class FakeUserStore:
    """Accounts in a dictionary, keyed the way the real table is."""

    accounts: dict[str, Account] = field(default_factory=dict[str, Account])
    minted: int = 0

    def get(self, user_id: str) -> User | None:
        found = [
            account.as_user()
            for account in self.accounts.values()
            if account.id == user_id
        ]
        return found[0] if found else None

    def get_by_username(self, username: str) -> Account | None:
        return self.accounts.get(username)

    def add_first_account(self, account: Account) -> None:
        if self.accounts:
            message = "this instance already has an account"
            raise FirstStartClosedError(message)
        self.accounts[account.username] = account

    def create(self, username: str, password_hash: str, role: str) -> Account:
        self.minted += 1
        account = Account(
            id=f"created-{self.minted}",
            username=username,
            role=Role(role),
            password_hash=password_hash,
        )
        self.accounts[username] = account
        return account

    def count(self) -> int:
        return len(self.accounts)

    def first_admin(self) -> User | None:
        admins = [
            account.as_user()
            for account in self.accounts.values()
            if account.role is Role.ADMIN
        ]
        return admins[0] if admins else None


@dataclass
class FakeSessionRecordStore:
    """Session rows in a dictionary."""

    rows: dict[str, Session] = field(default_factory=dict[str, Session])
    clock: FrozenClock | None = None
    minted: int = 0

    def create(
        self,
        user_id: str,
        expires_at: datetime,
        *,
        ip_address: str,
        user_agent: str,
    ) -> Session:
        del expires_at
        self.minted += 1
        assert self.clock is not None
        last_seen = self.clock.now()
        session = Session(
            id=f"session-{self.minted}",
            user_id=user_id,
            last_seen=last_seen,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.rows[session.id] = session
        return session

    def load(self, session_id: str) -> Session | None:
        return self.rows.get(session_id)

    def touch(
        self,
        record: Session,
        *,
        ip_address: str,
        user_agent: str,
        now: datetime,
    ) -> None:
        self.rows[record.id] = replace(
            record,
            last_seen=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    def delete(self, session_id: str) -> None:
        self.rows.pop(session_id, None)

    def delete_for_user(self, user_id: str) -> int:
        kept = {
            session_id: session
            for session_id, session in self.rows.items()
            if session.user_id != user_id
        }
        dropped = len(self.rows) - len(kept)
        self.rows.clear()
        self.rows.update(kept)
        return dropped

    def prune_overflow(self, user_id: str, max_sessions: int) -> list[str]:
        owned = sorted(
            (session for session in self.rows.values() if session.user_id == user_id),
            key=lambda session: (session.last_seen, session.id),
            reverse=True,
        )
        dropped = [session.id for session in owned[max_sessions:]]
        for session_id in dropped:
            self.rows.pop(session_id, None)
        return dropped


@dataclass
class FakeLoginAttemptStore:
    """Failures as a list of moments per name and address."""

    clock: FrozenClock
    failures: list[tuple[str, str, datetime]] = field(
        default_factory=list[tuple[str, str, datetime]],
    )

    def record(self, *, ip_address: str, username: str, success: bool) -> None:
        if success:
            return
        self.failures.append((username, ip_address, self.clock.now()))

    def count_recent_failures(
        self,
        *,
        ip_address: str,
        window_seconds: int,
        username: str | None = None,
    ) -> int:
        since = self.clock.now() - timedelta(seconds=window_seconds)
        if username is not None:
            return sum(
                1 for name, _, at in self.failures if name == username and at >= since
            )
        return sum(
            1
            for _, address, at in self.failures
            if address == ip_address and at >= since
        )


@dataclass(frozen=True, slots=True)
class MatchingLiveness:
    """The idle-window rule a test can freeze, matching the library's `<=`."""

    window: timedelta = IDLE_WINDOW

    def admits(self, session: Session, *, at: datetime) -> bool:
        return (at - session.last_seen).total_seconds() <= self.window.total_seconds()


@dataclass
class ReversibleHasher:
    """A hash a test can read, so no test ever needs the real cost of Argon2id."""

    marker: str = "hashed:"

    def hash(self, password: str) -> str:
        return f"{self.marker}{password}"

    def verify(self, password: str, stored_hash: str | None) -> bool:
        return stored_hash == self.hash(password)


@dataclass
class FrozenClock:
    """A clock only a test moves."""

    instant: datetime

    def now(self) -> datetime:
        return self.instant

    def advance(self, by: timedelta) -> None:
        self.instant += by


@dataclass
class CountingIdentifierFactory:
    """Ids a test can predict."""

    minted: int = 0

    def new_id(self) -> str:
        self.minted += 1
        return f"id-{self.minted}"


@dataclass
class MarkingCookieSigner:
    """A signature a test can forge on purpose."""

    marker: str = "signed-"

    def sign(self, session_id: str) -> str:
        return f"{self.marker}{session_id}"

    def session_id_from(self, cookie_value: str) -> str | None:
        if not cookie_value.startswith(self.marker):
            return None
        return cookie_value.removeprefix(self.marker)


@dataclass
class UserStoreThatLostTheRace(FakeUserStore):
    """Another first start has written; this one still sees the count it read."""

    def count(self) -> int:
        return 0


@dataclass
class FakeInstanceSettingsStore:
    """The one row of instance defaults, in memory."""

    saved: InstanceSettings | None = None

    def read(self) -> InstanceSettings | None:
        return self.saved

    def write(self, settings: InstanceSettings) -> None:
        self.saved = settings


@dataclass
class FakePersonPreferencesStore:
    """One entry per person who chose something of their own."""

    chosen: dict[str, PersonPreferences] = field(
        default_factory=dict[str, PersonPreferences],
    )

    def read(self, user_id: str) -> PersonPreferences | None:
        return self.chosen.get(user_id)

    def write(self, user_id: str, preferences: PersonPreferences) -> None:
        self.chosen[user_id] = preferences


@dataclass
class FakeSourceStore:
    """The sources an instance has."""

    sources: list[Source] = field(default_factory=list[Source])
    hashes: dict[str, bytes] = field(default_factory=dict[str, bytes])
    secrets: dict[str, str] = field(default_factory=dict[str, str])
    minted: int = 0

    def add(self, write: SourceWrite) -> Source | None:
        if any(
            source.name == write.name or source.url == write.url
            for source in self.sources
        ):
            return None
        self.minted += 1
        stored = Source(
            id=f"added-{self.minted}",
            name=write.name,
            url=write.url,
            ref=write.ref,
            secret_location=SecretLocation.STORED,
            owner_id=write.owner_id,
        )
        self.sources.append(stored)
        self.hashes[write.name] = write.hook_secret_hash
        self.secrets[stored.id] = write.access_secret
        return stored

    def hook_secret_hash(self, name: str) -> bytes | None:
        return self.hashes.get(name)

    def put_credential(self, source_id: str, secret: str) -> None:
        self.secrets[source_id] = secret
        self.sources = [
            replace(source, secret_location=SecretLocation.STORED)
            if source.id == source_id
            else source
            for source in self.sources
        ]

    def put_hook_secret_hash(self, name: str, digest: bytes) -> bool:
        if not any(source.name == name for source in self.sources):
            return False
        self.hashes[name] = digest
        return True

    def all(self) -> tuple[Source, ...]:
        return tuple(self.sources)


@dataclass
class FakeSourceRunStore:
    """Every run recorded so far, in the order it arrived."""

    recorded: list[SourceRun] = field(default_factory=list[SourceRun])

    def record(self, run: SourceRun) -> None:
        self.recorded.append(run)

    def newest(self, source_id: str) -> SourceRun | None:
        for run in reversed(self.recorded):
            if run.source_id == source_id:
                return run
        return None

    def recent(self, source_id: str) -> tuple[SourceRun, ...]:
        found = [run for run in reversed(self.recorded) if run.source_id == source_id]
        return tuple(found[:RECENT_SOURCE_RUNS])


@dataclass
class FakeConnectionChecker:
    """Answers the one canned result a test wired, whatever it was asked."""

    answer: ConnectionCheckResult = A_REACHABLE_CHECK

    def check(self, *, url: str, ref: str, secret: str) -> ConnectionCheckResult:
        return self.answer


@dataclass
class FakeDeckFolders:
    """What each source carries, or nothing where one cannot be read.

    A poll's commit and failure are controlled per source only where a test
    cares about the run they are recorded into; every other test only arranges
    `carried`.
    """

    carried: dict[str, tuple[DeckFolder, ...] | None] = field(
        default_factory=dict[str, tuple[DeckFolder, ...] | None],
    )
    commits: dict[str, str] = field(default_factory=dict[str, str])
    failures: dict[str, SourceRunFailure] = field(
        default_factory=dict[str, SourceRunFailure],
    )

    def folders(self, source: Source) -> SourcePoll:
        found = self.carried.get(source.id, ())
        if found is None:
            return SourcePoll(
                folders=None,
                commit=None,
                failure=self.failures.get(source.id, _AN_UNNAMED_FAILURE),
            )
        return SourcePoll(
            folders=found,
            commit=self.commits.get(source.id, _A_FETCHED_COMMIT),
            failure=None,
        )


@dataclass
class FakeDeckStore:
    """Deck rows in a dictionary, keyed by slug the way the table is."""

    kept: dict[str, Deck] = field(default_factory=dict[str, Deck])
    removed: set[str] = field(default_factory=set[str])

    def put(self, deck: Deck) -> bool:
        standing = self.kept.get(deck.slug)
        if standing is not None and standing.source_id != deck.source_id:
            return False
        # What a build wrote is the build's alone, the way the deck table's own
        # upsert leaves those columns untouched.
        self.kept[deck.slug] = replace(
            deck,
            build=None if standing is None else standing.build,
            attempt=None if standing is None else standing.attempt,
        )
        return True

    def put_build(self, slug: str, build: Build) -> None:
        self.kept[slug] = replace(self.kept[slug], build=build, attempt=None)

    def put_attempt(self, slug: str, attempt: BuildAttempt) -> None:
        self.kept[slug] = replace(self.kept[slug], attempt=attempt)

    def get(self, slug: str) -> Deck | None:
        return None if slug in self.removed else self.kept.get(slug)

    def all(self) -> tuple[Deck, ...]:
        return tuple(
            deck for slug, deck in self.kept.items() if slug not in self.removed
        )

    def mark_removed_except(
        self,
        present: frozenset[str],
        *,
        source_id: str,
        at: datetime,
    ) -> None:
        reconciled = {
            slug for slug, deck in self.kept.items() if deck.source_id == source_id
        }
        self.removed -= reconciled
        self.removed |= reconciled - present


@dataclass
class HeldDeckFolders:
    """A mirror read a test opens and closes, so overlapping reads are visible.

    Every read waits for the test to release it, and the double counts how many
    were inside at the same time.
    """

    found: tuple[DeckFolder, ...] = ()
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    reads: int = 0
    at_once: int = 0
    inside: int = 0
    counting: threading.Lock = field(default_factory=threading.Lock)

    def folders(self, source: Source) -> SourcePoll:
        self._enter()
        self.entered.set()
        self.release.wait(PATIENCE.total_seconds())
        with self.counting:
            self.inside -= 1
        return SourcePoll(folders=self.found, commit=_A_FETCHED_COMMIT, failure=None)

    def _enter(self) -> None:
        with self.counting:
            self.reads += 1
            self.inside += 1
            self.at_once = max(self.at_once, self.inside)


# Where a fake build writes, standing in for the root an instance keeps builds
# under; no test touches these paths, because the runner is the boundary the
# filesystem lives behind.
BUILDS_ROOT: Final = Path("/var/lib/presentator/builds")
_SOMEWHERE_ELSE: Final = Path("/var/lib/presentator/mirrors")


@dataclass
class FakeBuildRunner:
    """A build that answers with paths instead of running a toolchain.

    It can fail the way a broken deck does, with or without words, and it can
    answer with a place outside the root the way a deck that wrote its own
    output path would.
    """

    fails: bool = False
    says: str | None = None
    writes_outside_the_root: bool = False
    built: list[str] = field(default_factory=list[str])
    from_source: dict[str, str] = field(default_factory=dict[str, str])
    while_building: Callable[[Deck], None] | None = None

    def build(self, deck: Deck, *, source: Source) -> Artefacts | BuildFailure:
        self.built.append(deck.slug)
        self.from_source[deck.slug] = source.id
        if self.while_building is not None:
            self.while_building(deck)
        if self.fails:
            return BuildFailure(text=self.says)
        root = _SOMEWHERE_ELSE if self.writes_outside_the_root else BUILDS_ROOT
        written = root / deck.slug / deck.commit
        return Artefacts(directory=written / "talk", pdf=written / "deck.pdf")

    def holds(self, artefacts: Artefacts) -> bool:
        return all(
            written.is_relative_to(BUILDS_ROOT)
            for written in (artefacts.directory, artefacts.pdf)
        )


def some_words(*, language_tag: str, language_name: str) -> LobbyText:
    """A catalog whose every word is its own field name, in a named language."""
    named = LobbyText(**{field.name: field.name for field in fields(LobbyText)})
    return replace(named, language_tag=language_tag, language_name=language_name)
