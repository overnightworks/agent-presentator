"""The two tokens the agent service needs: the person's and presentator's own."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, NoReturn, assert_never

from agent_client.errors import raise_without_chain
from webauth.oidc import OidcRefusal
from webauth.oidc_login import (
    AccessTokenRefusal,
    AccessTokenRefusalReason,
    fresh_access_token,
)

from presentator.contracts.copresenter import (
    AccessRefusal,
    CoPresenterUnavailableError,
    PersonToken,
)

if TYPE_CHECKING:
    from webauth.config import WebAuthConfig
    from webauth.oidc import OidcRelyingParty
    from webauth.ports import SessionRecordStore

_LOGGER = logging.getLogger(__name__)


def _unavailable() -> NoReturn:
    raise_without_chain(CoPresenterUnavailableError())


def _thread_failed(error: Exception) -> NoReturn:
    # The error is foreign and may hold a token, so only its type is shown.
    _LOGGER.warning("agent token source failed: %s", type(error).__name__)
    _unavailable()


class PersonAccessTokens:
    """A signed-in person's fresh access token, from their session."""

    def __init__(
        self,
        relying_party: OidcRelyingParty,
        sessions: SessionRecordStore,
        config: WebAuthConfig,
    ) -> None:
        """Renew through this client, read and rotate in these sessions."""
        self._relying_party = relying_party
        self._sessions = sessions
        self._config = config

    async def fresh(self, session_id: str) -> PersonToken | AccessRefusal:
        """The session's person's token, or why it has none."""
        try:
            token = await asyncio.to_thread(
                fresh_access_token,
                self._relying_party,
                session_id,
                session_transaction=lambda: nullcontext(self._sessions),
                config=self._config,
            )
        # Every thread failure is one unavailable error.
        except Exception as error:  # noqa: BLE001
            _thread_failed(error)
        if isinstance(token, AccessTokenRefusal):
            return _refusal_of(token)
        return PersonToken(token.get_secret_value())


def _refusal_of(refusal: AccessTokenRefusal) -> AccessRefusal:
    match refusal.reason:
        case AccessTokenRefusalReason.SIGN_IN_AGAIN:
            return AccessRefusal.SIGN_IN_AGAIN
        case AccessTokenRefusalReason.SESSION_ENDED:
            return AccessRefusal.SESSION_ENDED
        case AccessTokenRefusalReason.ISSUER_UNAVAILABLE:
            _LOGGER.warning("issuer gave no access token: %s", refusal.issuer_refusal)
            return AccessRefusal.ISSUER_UNAVAILABLE
        case _:
            assert_never(refusal.reason)


class WorkloadToken:
    """Presentator's own access token, fetched anew on every call."""

    def __init__(self, relying_party: OidcRelyingParty) -> None:
        """Fetch through this client."""
        self._relying_party = relying_party

    async def __call__(self) -> str:
        """A workload access token; any refusal or failure is one unavailable error."""
        try:
            token = await asyncio.to_thread(self._relying_party.workload_access_token)
        # Every thread failure is one unavailable error.
        except Exception as error:  # noqa: BLE001
            _thread_failed(error)
        if isinstance(token, OidcRefusal):
            _LOGGER.warning("issuer gave no workload token: %s", token)
            _unavailable()
        return token.get_secret_value()
