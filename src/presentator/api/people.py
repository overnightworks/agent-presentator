"""The admin route that erases one person, behind hal's access token alone.

The path prefix takes a Keycloak access token and no cookie (ADR 0003); the
body is the shared `people_erasure` shape of edge's people contract.
"""

import logging
import math
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Final, assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.responses import Response
from webauth.middleware.bearer_gate import bearer_caller
from webauth.ports import BearerCaller

from presentator.application.people import People
from presentator.contracts.people import (
    KindCount,
    PersonBusy,
    PersonDiskLeftover,
    PersonErased,
    PersonErasure,
    PersonKind,
)

ADMIN_PREFIX: Final = "/api/admin/"
_PEOPLE_ERASURE_VERSION: Final = 1

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminPeople:
    """The people use case and the clients whose tokens may call it, together.

    A route without the clients that admit its callers cannot be composed.
    """

    people: People
    admitted_clients: frozenset[str]


def _counts(kinds: tuple[KindCount, ...]) -> list[dict[str, object]]:
    return [{"kind": counted.kind.value, "count": counted.count} for counted in kinds]


def _envelope(app: str, subject: str) -> dict[str, object]:
    return {
        "people_erasure": _PEOPLE_ERASURE_VERSION,
        "app": app,
        "subject": subject,
    }


def _answer(
    outcome: PersonErasure, *, app: str, subject: str, dry_run: bool, wait_seconds: int
) -> JSONResponse:
    body = _envelope(app, subject)
    match outcome:
        case PersonErased(kinds=kinds):
            body |= {
                "dry_run": dry_run,
                "kinds": _counts(kinds),
                "affects_others": [],
            }
            return JSONResponse(body)
        case PersonBusy(running=running):
            return JSONResponse(
                body | {"busy": _counts(running)},
                HTTPStatus.CONFLICT,
                headers={"Retry-After": str(wait_seconds)},
            )
        case PersonDiskLeftover():
            leftover = KindCount(kind=PersonKind.SOURCES, count=1)
            return JSONResponse(
                body | {"leftover": _counts((leftover,))},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        case _:
            assert_never(outcome)


def _tallied(kinds: tuple[KindCount, ...]) -> str:
    return ", ".join(f"{one.kind.value}={one.count}" for one in kinds) or "nothing"


def _audited(outcome: PersonErasure, *, dry_run: bool) -> None:
    """One line per call: what was asked, how it ended, how many; nobody named."""
    operation = "dry run" if dry_run else "erase"
    match outcome:
        case PersonErased(kinds=kinds):
            _log.info("person %s: erased %s", operation, _tallied(kinds))
        case PersonBusy(running=running):
            _log.info("person %s: busy with %s", operation, _tallied(running))
        case PersonDiskLeftover():
            _log.info("person %s: disk leftover", operation)
        case _:
            assert_never(outcome)


def people_routes(*, admin: AdminPeople, app: str) -> APIRouter:
    """The erase route of the people contract, for the lobby factory to include."""
    people = admin.people
    wait_seconds = math.ceil(people.lock_wait.total_seconds())
    router = APIRouter()

    # A plain `def`: the wait for a running build blocks a pool thread, never the loop.
    @router.delete(f"{ADMIN_PREFIX}people/{{subject}}")
    def erase_person(
        subject: str,
        caller: Annotated[BearerCaller, Depends(bearer_caller)],
        *,
        dry_run: bool = False,
    ) -> Response:
        del caller
        outcome = people.erase_person(subject, dry_run=dry_run)
        _audited(outcome, dry_run=dry_run)
        return _answer(
            outcome,
            app=app,
            subject=subject,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
        )

    return router
