"""The address a source's host calls to say that a push happened.

It reads no payload (ADR 0010), so a hosted service, a self-hosted one, and a
`post-receive` hook on a bare repository are the same caller; the secret the
call carries is what authenticates it. The address exists only while a secret
arms it, and every call it does not recognise — a wrong secret, no secret,
another name, a stray path below it — is answered alike, so nobody learns here
which sources this instance has.
"""

from dataclasses import dataclass
from hmac import compare_digest
from http import HTTPMethod, HTTPStatus
from typing import Annotated, Final

from fastapi import APIRouter, Header, Response

from presentator.application.decks import Decks

HOOKS_PATH: Final = "/hooks"
# What a source's host may address. The hook itself, not the router, decides
# which of those calls is the one it exists for, so an unknown name, a trailing
# slash, and a deeper path leave with the same answer.
HOOK_CALLS: Final = f"{HOOKS_PATH}/"

_ANY_CALL: Final = f"{HOOK_CALLS}{{source:path}}"
_BEARER: Final = "Bearer "
_NO_SECRET: Final = b""


@dataclass(frozen=True, slots=True, kw_only=True)
class _FetchHook:
    """Refreshes the one source this instance is configured with."""

    decks: Decks
    source: str
    secret: str

    def called(
        self,
        source: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Fetch now when the call names the source and carries its secret."""
        if not self._belongs_to_the_source(source, authorization):
            return Response(status_code=HTTPStatus.NOT_FOUND)
        self.decks.refresh()
        return Response(status_code=HTTPStatus.NO_CONTENT)

    def _belongs_to_the_source(self, source: str, authorization: str | None) -> bool:
        # Both comparisons run whatever the first one answers, so the time an
        # answer takes tells a caller neither the secret nor which names exist.
        named = compare_digest(source.encode(), self.source.encode())
        authenticated = compare_digest(
            _offered_secret(authorization),
            self.secret.encode(),
        )
        return named and authenticated


def _offered_secret(authorization: str | None) -> bytes:
    if authorization is None or not authorization.startswith(_BEARER):
        return _NO_SECRET
    return authorization.removeprefix(_BEARER).encode()


def fetch_hook(*, decks: Decks, source: str, secret: str) -> APIRouter:
    """The `fetch now` route for a source, answering every other call alike."""
    hook = _FetchHook(decks=decks, source=source, secret=secret)
    router = APIRouter()
    router.add_api_route(_ANY_CALL, hook.called, methods=[HTTPMethod.POST])
    return router
