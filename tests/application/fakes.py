"""In-memory stands-in for the ports, so the use cases run pure."""

import threading
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from presentator.contracts.decks import (
    Artefacts,
    Build,
    Deck,
    DeckFolder,
    Source,
    SourcePoll,
    SourceRun,
    SourceRunFailure,
)
from presentator.contracts.models import (
    Credentials,
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


@dataclass
class FakeUserStore:
    """Accounts in a dictionary, keyed the way the real table is."""

    accounts: dict[str, Credentials] = field(default_factory=dict[str, Credentials])

    def get(self, user_id: str) -> User | None:
        found = [
            credentials.user
            for credentials in self.accounts.values()
            if credentials.user.id == user_id
        ]
        return found[0] if found else None

    def credentials_for(self, username: str) -> Credentials | None:
        return self.accounts.get(username)

    def add_first_account(self, credentials: Credentials) -> None:
        if self.accounts:
            message = "this instance already has an account"
            raise FirstStartClosedError(message)
        self.accounts[credentials.user.username] = credentials

    def count(self) -> int:
        return len(self.accounts)

    def first_admin(self) -> User | None:
        admins = [
            credentials.user
            for credentials in self.accounts.values()
            if credentials.user.role is Role.ADMIN
        ]
        return admins[0] if admins else None


@dataclass
class FakeSessionRecordStore:
    """Session rows in a dictionary."""

    rows: dict[str, Session] = field(default_factory=dict[str, Session])

    def get(self, session_id: str) -> Session | None:
        return self.rows.get(session_id)

    def put(self, session: Session) -> None:
        self.rows[session.id] = session

    def remove(self, session_id: str) -> None:
        self.rows.pop(session_id, None)


@dataclass
class FakeLoginAttemptStore:
    """Failures as a list of moments per name."""

    failures: list[tuple[str, datetime]] = field(
        default_factory=list[tuple[str, datetime]],
    )

    def record_failure(self, username: str, *, at: datetime) -> None:
        self.failures.append((username, at))

    def failure_count(self, username: str, *, since: datetime) -> int:
        return sum(1 for name, at in self.failures if name == username and at >= since)


@dataclass
class ReversibleHasher:
    """A hash a test can read, so no test ever needs the real cost of Argon2id."""

    marker: str = "hashed:"

    def hash(self, password: str) -> str:
        return f"{self.marker}{password}"

    def verify(self, password: str, password_hash: str | None) -> bool:
        return password_hash == self.hash(password)


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
    """The sources an instance has, and the one its configuration seeds."""

    sources: list[Source] = field(default_factory=list[Source])
    seeds: Source | None = None

    def seed(self) -> None:
        if self.seeds is not None and self.seeds not in self.sources:
            self.sources.append(self.seeds)

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
        self.kept[deck.slug] = replace(
            deck,
            build=None if standing is None else standing.build,
        )
        return True

    def put_build(self, slug: str, build: Build) -> None:
        self.kept[slug] = replace(self.kept[slug], build=build)

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

    It can fail the way a broken deck does, and it can answer with a place
    outside the root the way a deck that wrote its own output path would.
    """

    fails: bool = False
    writes_outside_the_root: bool = False
    built: list[str] = field(default_factory=list[str])
    from_source: dict[str, str] = field(default_factory=dict[str, str])

    def build(self, deck: Deck, *, source: Source) -> Artefacts | None:
        self.built.append(deck.slug)
        self.from_source[deck.slug] = source.id
        if self.fails:
            return None
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
