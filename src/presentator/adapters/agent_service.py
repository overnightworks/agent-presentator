"""Starts one run on the operator's agent service and streams its text."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import httpx2
from agent_client.agents import load_agent
from agent_client.errors import raise_without_chain
from agent_client.run_stream import StreamViolation
from agent_client.sse import RunStreamDecoder
from agent_client.wire import (
    AssistantText,
    Cancelled,
    Failed,
    RejectionCode,
    RunRejection,
    TurnMessage,
    TurnRole,
)
from pydantic import ValidationError

from presentator.contracts.copresenter import (
    CoPresenterUnavailableError,
    NotEnabledError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)
_AGENT_FILE: Final = Path(__file__).parent.parent / "agents" / "copresenter.md"
_EVENT_STREAM: Final = "text/event-stream"
_BEARER_TOKEN: Final = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_WHOLE_SECONDS: Final = re.compile(r"\d{1,9}", re.ASCII)
# Read is four heartbeats of the service's fifteen seconds.
_TIMEOUT: Final = httpx2.Timeout(connect=5, write=5, pool=5, read=60)
_RUNS_PATH: Final = "/runs"
_MILLISECONDS: Final = 1000

type TokenSource = Callable[[], Awaitable[str]]


def _fail(reason: str) -> NoReturn:
    _LOGGER.warning("agent service run failed: %s", reason)
    raise_without_chain(CoPresenterUnavailableError())


def _retry_after_seconds(response: httpx2.Response) -> int | None:
    shown = response.headers.get("retry-after", "")
    return int(shown) if _WHOLE_SECONDS.fullmatch(shown) else None


def _rejection_code(body: bytes) -> RejectionCode | None:
    try:
        return RunRejection.model_validate_json(body).code
    except ValidationError:
        return None


def _is_event_stream(response: httpx2.Response) -> bool:
    media_type = response.headers.get("content-type", "").partition(";")[0]
    return media_type.strip().lower() == _EVENT_STREAM


async def _bearer_token(source: TokenSource) -> str:
    # A source's exception is foreign and may hold the token, so it is never
    # raised, logged or chained. Cancellation and process control pass through.
    try:
        token = await source()
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
        error.__cause__ = error.__context__ = None
        raise
    except BaseException as error:  # noqa: BLE001 - the boundary sanitises every other supplier failure; gather routes measured worse
        _fail(f"token source raised {type(error).__name__}")
    if _BEARER_TOKEN.fullmatch(token) is None:
        _fail("token is not a bearer token")
    return token


async def _refuse(response: httpx2.Response) -> NoReturn:
    body = await response.aread()
    code = None if response.is_success else _rejection_code(body)
    seconds = _retry_after_seconds(response)
    shown = None if code is None else code.value
    _LOGGER.warning("agent service refused the run: %s retry_after=%s", shown, seconds)
    if code is RejectionCode.NOT_ENABLED:
        raise_without_chain(NotEnabledError())
    raise_without_chain(CoPresenterUnavailableError())


@dataclass
class _RunTally:
    """What an opened run showed, without a word of its content."""

    started: float
    clock: Callable[[], float]
    events: dict[str, int] = field(default_factory=dict[str, int])
    violation: str | None = None
    first_text: float | None = None

    def note(self, item: object) -> None:
        name = type(item).__name__
        self.events[name] = self.events.get(name, 0) + 1
        if isinstance(item, StreamViolation):
            self.violation = item.kind.value
        elif isinstance(item, AssistantText) and self.first_text is None:
            self.first_text = self.clock()

    def log_summary(self) -> None:
        ended = self.clock()
        _LOGGER.info(
            "agent service run summary: events=%s violation=%s "
            "first_text_ms=%s end_ms=%s",
            ",".join(f"{name}:{count}" for name, count in self.events.items()),
            self.violation,
            None if self.first_text is None else self._milliseconds(self.first_text),
            self._milliseconds(ended),
        )

    def _milliseconds(self, instant: float) -> int:
        return round((instant - self.started) * _MILLISECONDS)


async def _texts(response: httpx2.Response, tally: _RunTally) -> AsyncGenerator[str]:
    decoder = RunStreamDecoder()
    try:
        async for chunk in response.aiter_bytes():
            for item in decoder.feed(chunk):
                tally.note(item)
                if (text := _text_of(item)) is not None:
                    yield text
        for item in decoder.close():
            tally.note(item)
            _text_of(item)
    except httpx2.HTTPError as error:
        _fail(f"transport raised {type(error).__name__}")


def _text_of(item: object) -> str | None:
    match item:
        case AssistantText(text=text):
            return text
        case Failed(code=code):
            _fail(f"failed {code.value}")
        case Cancelled():
            _fail("cancelled")
        case StreamViolation(kind=kind):
            _fail(f"stream violation {kind.value}")
        case _:
            return None


class ServiceAnswerer:
    """Answer one question with a run of the agent service's co-presenter purpose."""

    def __init__(
        self,
        *,
        origin: str,
        workload_token: TokenSource,
        transport: Callable[[], httpx2.AsyncBaseTransport] = httpx2.AsyncHTTPTransport,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Keep where the service is, who the workload is and how to reach it."""
        self._runs_url = httpx2.URL(origin).join(_RUNS_PATH)
        self._workload_token = workload_token
        self._transport = transport
        self._monotonic = monotonic
        self._agent = load_agent(_AGENT_FILE, [])

    @asynccontextmanager
    async def answer(
        self, *, message: str, person_token: TokenSource
    ) -> AsyncGenerator[AsyncIterator[str]]:
        """Stream the run's text; any refusal or failure is one unavailable error."""
        body = self._run_request(message)
        person = await _bearer_token(person_token)
        workload = await _bearer_token(self._workload_token)
        async with AsyncExitStack() as scope:
            try:
                client = await scope.enter_async_context(
                    httpx2.AsyncClient(
                        transport=self._transport(), trust_env=False, timeout=_TIMEOUT
                    )
                )
                started = self._monotonic()
                response = await scope.enter_async_context(
                    client.stream(
                        "POST",
                        self._runs_url,
                        content=body,
                        headers={
                            "Authorization": f"Bearer {person}",
                            "Agent-Workload-Token": workload,
                            "Accept": _EVENT_STREAM,
                            "Content-Type": "application/json",
                        },
                    )
                )
                if response.status_code != HTTPStatus.OK or not _is_event_stream(
                    response
                ):
                    await _refuse(response)
            except httpx2.HTTPError as error:
                _fail(f"transport raised {type(error).__name__}")
            tally = _RunTally(started, self._monotonic)
            texts = _texts(response, tally)
            try:
                yield texts
            finally:
                await texts.aclose()
                tally.log_summary()

    def _run_request(self, message: str) -> str:
        try:
            return self._agent.request(
                [TurnMessage(role=TurnRole.USER, text=message)]
            ).model_dump_json()
        except (ValidationError, ValueError) as error:
            _fail(f"request invalid: {type(error).__name__}")
