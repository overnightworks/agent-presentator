"""A browser that leaves while the agent service is still answering."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from fastapi import FastAPI

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.api.copresenter import CoPresenterSurface, add_copresenter_routes
from presentator.application.copresenter import AskLimiter, AskTicket
from tests.adapters.agent_service_support import Wire
from tests.api.copresenter_support import (
    A_DECK,
    ASK,
    HANGING_AFTER_THE_FIRST_WORDS,
    SPOKEN_QUESTION,
    WORKLOAD,
    PrivateFakes,
    a_use,
    the_admins_decks,
)
from tests.api.lobby import A_TALK_ADDRESS, AN_AGENT_SERVICE, NOW
from tests.api.talk_support import A
from tests.application.fakes import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Scope


def _a_hanging_app(
    wire: Wire, limiter: AskLimiter, admission: Callable[[object], FixedSession]
) -> FastAPI:
    app = FastAPI()
    add_copresenter_routes(
        app,
        surface=CoPresenterSurface(
            use=replace(
                a_use(PrivateFakes(), the_admins_decks()),
                answers=ServiceAnswerer(
                    origin=AN_AGENT_SERVICE,
                    workload_token=a_workload_token,
                    transport=wire.transport,
                ),
            ),
            limiter=limiter,
            browser_origin=A_TALK_ADDRESS,
        ),
        admission=admission,
    )
    return app


def test_a_browser_that_leaves_mid_answer_closes_the_services_connection() -> None:
    """Cell 18."""
    wire = Wire(HANGING_AFTER_THE_FIRST_WORDS)
    limiter = AskLimiter(clock=FrozenClock(instant=NOW))
    app = _a_hanging_app(wire, limiter, lambda _connection: FixedSession())

    asyncio.run(_ask_and_leave_after_the_first_words(app))

    assert wire.response_stream.closed.is_set()
    assert isinstance(limiter.admit(FixedSession().session_id), AskTicket)


def test_the_admission_runs_off_the_event_loop_thread() -> None:
    """Decision 5: the session read of the admission belongs to the thread hop."""
    admitted_on: list[int] = []

    def admission(_connection: object) -> FixedSession:
        admitted_on.append(threading.get_ident())
        return FixedSession()

    app = _a_hanging_app(
        Wire(HANGING_AFTER_THE_FIRST_WORDS),
        AskLimiter(clock=FrozenClock(instant=NOW)),
        admission,
    )

    asyncio.run(_ask_and_leave_after_the_first_words(app))

    assert admitted_on
    assert threading.get_ident() not in admitted_on


async def a_workload_token() -> str:
    return WORKLOAD


@dataclass(frozen=True)
class FixedSession:
    """The admitted session of a browser."""

    session_id: str = "session"
    user_id: str = A.id
    is_admin: bool = True


async def _ask_and_leave_after_the_first_words(app: ASGIApp) -> None:
    first_words_sent = asyncio.Event()
    messages: list[Message] = [
        {
            "type": "http.request",
            "body": json.dumps({**SPOKEN_QUESTION, "deck": A_DECK}).encode(),
        }
    ]

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        await first_words_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_words_sent.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": ASK,
        "raw_path": ASK.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"talk.testserver"),
            (b"content-type", b"application/json"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
