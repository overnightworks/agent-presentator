"""In-memory stands-in for the ports, so the use cases run pure."""

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from presentator.contracts.decks import (
    Artefacts,
    Build,
    Deck,
    DeckFolder,
    Source,
)
from presentator.contracts.models import (
    Credentials,
    FirstStartClosedError,
    Role,
    Session,
    User,
)

# How long one thread waits for another before a test calls the run stuck.
PATIENCE: Final = timedelta(seconds=5)


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
class FakeSourceStore:
    """The one source an installation would have configured."""

    source: Source | None = None

    def configured(self) -> Source | None:
        return self.source


@dataclass
class FakeDeckFolders:
    """The folders a source carries, or nothing when it cannot be read."""

    found: tuple[DeckFolder, ...] | None = ()

    def folders(self, source: Source) -> tuple[DeckFolder, ...] | None:
        return self.found


@dataclass
class FakeDeckStore:
    """Deck rows in a dictionary, keyed by slug the way the table is."""

    kept: dict[str, Deck] = field(default_factory=dict[str, Deck])
    removed: set[str] = field(default_factory=set[str])

    def put(self, deck: Deck) -> None:
        standing = self.kept.get(deck.slug)
        self.kept[deck.slug] = replace(
            deck,
            build=None if standing is None else standing.build,
        )

    def put_build(self, slug: str, build: Build) -> None:
        self.kept[slug] = replace(self.kept[slug], build=build)

    def get(self, slug: str) -> Deck | None:
        return None if slug in self.removed else self.kept.get(slug)

    def all(self) -> tuple[Deck, ...]:
        return tuple(
            deck for slug, deck in self.kept.items() if slug not in self.removed
        )

    def mark_removed_except(self, present: frozenset[str], *, at: datetime) -> None:
        self.removed = {slug for slug in self.kept if slug not in present}


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

    def folders(self, source: Source) -> tuple[DeckFolder, ...]:
        self._enter()
        self.entered.set()
        self.release.wait(PATIENCE.total_seconds())
        with self.counting:
            self.inside -= 1
        return self.found

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

    def build(self, deck: Deck, *, source: Source) -> Artefacts | None:
        self.built.append(deck.slug)
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
