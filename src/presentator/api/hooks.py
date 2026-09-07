"""The address a source's host calls to say that a push happened.

It reads no payload (ADR 0010), so a hosted service, a self-hosted one, and a
`post-receive` hook on a bare repository are the same caller; the secret the
call carries is what authenticates it. The address is `POST /sources/{name}/fetch`
for a stored source, and every call it does not recognise — a wrong secret, no
secret, another name, a trailing slash, a deeper path — is answered alike, so
nobody learns here which sources this instance has.
"""

from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from typing import Annotated, Final

from fastapi import APIRouter, Header, Response

from presentator.application.decks import Decks

HOOKS_PATH: Final = "/sources"
# What a source's host may address. The exemption is this prefix, never one
# exact string: a trailing slash on the named fetch would otherwise redirect
# into the session guard. The hook itself decides which of those calls is the
# one it exists for.
HOOK_CALLS: Final = f"{HOOKS_PATH}/"

_ANY_CALL: Final = f"{HOOK_CALLS}{{rest:path}}"
_FETCH: Final = "fetch"
_BEARER: Final = "Bearer "


@dataclass(frozen=True, slots=True, kw_only=True)
class _FetchHook:
    """Refreshes the one source whose name and webhook secret the call carries."""

    decks: Decks

    def called(
        self,
        rest: str,
        authorization: Annotated[str | None, Header()] = None,
        x_gitlab_token: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Fetch now when the call names a stored source and carries its secret."""
        if not self.decks.accept_hook(
            _named_source(rest),
            _offered_secret(authorization, x_gitlab_token),
        ):
            return Response(status_code=HTTPStatus.NOT_FOUND)
        return Response(status_code=HTTPStatus.NO_CONTENT)


def hook_address(name: str) -> str:
    """The path a host posts to for that source, as the created screen prints it."""
    return f"{HOOK_CALLS}{name}/{_FETCH}"


def _named_source(rest: str) -> str:
    """The stored name the path names, or nothing when it is not `{name}/fetch`.

    The name is matched against stored names later and is never joined into a
    path; a trailing slash or a deeper path is not this shape.
    """
    name, separator, tail = rest.partition("/")
    if separator and tail == _FETCH and name:
        return name
    return ""


def _offered_secret(authorization: str | None, gitlab_token: str | None) -> str:
    """The secret the call carried, from either header a host may send."""
    if authorization is not None and authorization.startswith(_BEARER):
        return authorization.removeprefix(_BEARER)
    if gitlab_token is not None:
        return gitlab_token
    return ""


def fetch_hook(*, decks: Decks) -> APIRouter:
    """The fetch-now route for every stored source, answering other calls alike."""
    hook = _FetchHook(decks=decks)
    router = APIRouter()
    router.add_api_route(_ANY_CALL, hook.called, methods=[HTTPMethod.POST])
    return router
