"""First start, the live session, and logout.

Login admission and the failure budget are the library's: the routes call
`judge_credentials` and `login_attempt_budget`. The window length is the
operator's ruling of 06.09.2026 on issue #8: a login survives a talk, and
repeated failures are refused for a while.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from presentator.contracts.models import Account, Role, Session, User
from presentator.ports.clock import Clock
from presentator.ports.identity import (
    IdentifierFactory,
    LoginAttemptStore,
    PasswordHasher,
    SessionCookieSigner,
    SessionLiveness,
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
    liveness: SessionLiveness

    def first_start_is_open(self) -> bool:
        """First start is offered while the instance has no account at all."""
        return self.users.count() == 0

    def create_first_admin(
        self,
        *,
        username: str,
        password: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> str:
        """Return the cookie for the new admin, or refuse if one already exists.

        Hashing happens before the store is asked, so the slow part is outside
        the write the store takes to keep first start single.
        """
        account = Account(
            id=self.identifiers.new_id(),
            username=username,
            role=Role.ADMIN,
            password_hash=self.hasher.hash(password),
        )
        self.users.add_first_account(account)
        return self.open_session(
            account.as_user(),
            ip_address=ip_address,
            user_agent=user_agent,
        )

    def account_named(self, username: str) -> Account | None:
        """The stored account for the name someone typed, if one exists."""
        return self.users.get_by_username(username)

    def note_failed_login(self, *, ip_address: str, username: str) -> None:
        """Spend one of the failure budget for this name and address."""
        self.attempts.record(ip_address=ip_address, username=username, success=False)

    def open_session(
        self,
        user: User,
        *,
        ip_address: str,
        user_agent: str,
    ) -> str:
        """Return the cookie for a fresh session of this person."""
        session = self.sessions.create(
            user.id,
            self.clock.now() + IDLE_WINDOW,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return self.cookies.sign(session.id)

    def signed_in_user(
        self,
        cookie_value: str,
        *,
        ip_address: str = "",
        user_agent: str = "",
    ) -> User | None:
        """Return who the cookie stands for, sliding the idle window along."""
        session = self._live_session(cookie_value)
        if session is None:
            return None
        self.sessions.touch(
            session,
            ip_address=ip_address,
            user_agent=user_agent,
            now=self.clock.now(),
        )
        return self.users.get(session.user_id)

    def log_out(self, cookie_value: str) -> None:
        """End the session behind the cookie so the value cannot come back."""
        session_id = self.cookies.session_id_from(cookie_value)
        if session_id is not None:
            self.sessions.delete(session_id)

    def _live_session(self, cookie_value: str) -> Session | None:
        session_id = self.cookies.session_id_from(cookie_value)
        if session_id is None:
            return None
        session = self.sessions.load(session_id)
        if session is None:
            return None
        if not self.liveness.admits(session, at=self.clock.now()):
            self.sessions.delete(session_id)
            return None
        return session
