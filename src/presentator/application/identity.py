"""First start, login, the throttle, the live session, and logout.

The window rules are the operator's ruling of 06.09.2026 on issue #8: a login
survives a talk, and repeated failures are refused for a while.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from presentator.contracts.models import Credentials, Role, Session, User
from presentator.ports.clock import Clock
from presentator.ports.identity import (
    IdentifierFactory,
    LoginAttemptStore,
    PasswordHasher,
    SessionCookieSigner,
    SessionRecordStore,
    UserStore,
)

IDLE_WINDOW: Final = timedelta(hours=12)
FAILURE_WINDOW: Final = timedelta(minutes=5)
FAILURES_BEFORE_THROTTLE: Final = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class Identity:
    """Every use case the lobby has around who is signed in."""

    users: UserStore
    sessions: SessionRecordStore
    attempts: LoginAttemptStore
    hasher: PasswordHasher
    clock: Clock
    identifiers: IdentifierFactory
    cookies: SessionCookieSigner

    def first_start_is_open(self) -> bool:
        """First start is offered while the instance has no account at all."""
        return self.users.count() == 0

    def create_first_admin(self, *, username: str, password: str) -> str:
        """Return the cookie for the new admin, or refuse if one already exists.

        Hashing happens before the store is asked, so the slow part is outside
        the write the store takes to keep first start single.
        """
        admin = User(
            id=self.identifiers.new_id(),
            username=username,
            role=Role.ADMIN,
        )
        credentials = Credentials(
            user=admin,
            password_hash=self.hasher.hash(password),
        )
        self.users.add_first_account(credentials)
        return self._open_session(admin)

    def log_in(self, *, username: str, password: str) -> str | None:
        """Return the cookie for a fresh session, or nothing when login is refused."""
        now = self.clock.now()
        if self._is_throttled(username, now=now):
            return None
        credentials = self.users.credentials_for(username)
        verified = self.hasher.verify(
            password,
            None if credentials is None else credentials.password_hash,
        )
        if credentials is None or not verified:
            self.attempts.record_failure(username, at=now)
            return None
        return self._open_session(credentials.user)

    def signed_in_user(self, cookie_value: str) -> User | None:
        """Return who the cookie stands for, sliding the idle window along."""
        session = self._live_session(cookie_value)
        if session is None:
            return None
        self.sessions.put(replace(session, last_seen=self.clock.now()))
        return self.users.get(session.user_id)

    def log_out(self, cookie_value: str) -> None:
        """End the session behind the cookie so the value cannot come back."""
        session_id = self.cookies.session_id_from(cookie_value)
        if session_id is not None:
            self.sessions.remove(session_id)

    def _is_throttled(self, username: str, *, now: datetime) -> bool:
        failures = self.attempts.failure_count(
            username,
            since=now - FAILURE_WINDOW,
        )
        return failures >= FAILURES_BEFORE_THROTTLE

    def _live_session(self, cookie_value: str) -> Session | None:
        session_id = self.cookies.session_id_from(cookie_value)
        if session_id is None:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if not session.is_alive(at=self.clock.now(), idle_window=IDLE_WINDOW):
            self.sessions.remove(session_id)
            return None
        return session

    def _open_session(self, user: User) -> str:
        session = Session(
            id=self.identifiers.new_id(),
            user_id=user.id,
            last_seen=self.clock.now(),
        )
        self.sessions.put(session)
        return self.cookies.sign(session.id)
