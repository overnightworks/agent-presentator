"""The live session and logout.

A sign-in lasts at most `SESSION_LIFETIME` from the moment it opened, however
often it is used.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from presentator.contracts.models import Session, User
from presentator.ports.clock import Clock
from presentator.ports.identity import (
    SessionLiveness,
    SessionRecordStore,
    UserStore,
)

SESSION_LIFETIME: Final = timedelta(hours=12)


@dataclass(frozen=True, slots=True, kw_only=True)
class Identity:
    """Every use case the lobby has around who is signed in.

    A session id is this use case's own currency; turning one into the
    signed cookie a browser carries, and back, is `webauth`'s (issue #105).
    """

    users: UserStore
    sessions: SessionRecordStore
    clock: Clock
    liveness: SessionLiveness

    def signed_in_user(
        self,
        session_id: str,
        *,
        ip_address: str = "",
        user_agent: str = "",
    ) -> User | None:
        """Return who the session id stands for, sliding the idle window along."""
        session = self._live_session(session_id)
        if session is None:
            return None
        renewed = self.sessions.touch(
            session,
            ip_address=ip_address,
            user_agent=user_agent,
            now=self.clock.now(),
        )
        if not renewed:
            return None
        return self.users.get(session.user_id)

    def log_out(self, session_id: str) -> None:
        """End the session so the id cannot come back."""
        if session_id:
            self.sessions.delete(session_id)

    def _live_session(self, session_id: str) -> Session | None:
        if not session_id:
            return None
        session = self.sessions.load(session_id)
        if session is None:
            return None
        if not self.liveness.admits(session, at=self.clock.now()):
            self.sessions.delete(session_id)
            return None
        return session
