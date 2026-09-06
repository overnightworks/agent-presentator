"""In-memory stands-in for the ports, so the use cases run pure."""

from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta

from presentator.contracts.models import (
    Credentials,
    FirstStartClosedError,
    Session,
    User,
)
from presentator.contracts.preferences import InstanceSettings, PersonPreferences
from presentator.contracts.text import LobbyText


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


def some_words(*, language_tag: str, language_name: str) -> LobbyText:
    """A catalog whose every word is its own field name, in a named language."""
    named = LobbyText(**{field.name: field.name for field in fields(LobbyText)})
    return replace(named, language_tag=language_tag, language_name=language_name)
