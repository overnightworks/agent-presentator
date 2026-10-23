"""Fakes and builders the service answerer's tests share: a wire, a service, tokens."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, NoReturn
from uuid import UUID, uuid4

import httpx2
from agent_client.sse import encode_event
from agent_client.wire import (
    AssistantText,
    Cancelled,
    Failed,
    FailureCode,
    Heartbeat,
    RejectionCode,
    RunRejection,
    RunRequest,
    RunStarted,
    Succeeded,
)
from fastapi import FastAPI, Header
from fastapi.responses import Response, StreamingResponse

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.contracts.copresenter import CoPresenterUnavailableError
from tests.adapters.serving import Serving, loopback_listener

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    import pytest
    from agent_client.wire import RunEvent

ORIGIN = "http://agent-service.test"
PERSON = "person-token-MARKER"
WORKLOAD = "workload-token-MARKER"
QUESTION = "question-MARKER"
SERVICE_MESSAGE = "service-message-MARKER"
CONTEXT = "context-MARKER"
VIOLATION_DETAIL = "violation-MARKER"
MARKERS = (
    PERSON,
    WORKLOAD,
    QUESTION,
    SERVICE_MESSAGE,
    VIOLATION_DETAIL,
    CONTEXT,
)
EVENT_STREAM = {"content-type": "text/event-stream"}
LOGGER = "presentator.adapters.agent_service"

type Step = Callable[[UUID, int], RunEvent]


def run_started() -> Step:
    return lambda run, _seq: RunStarted(v=2, run_id=run, seq=0, heartbeat_interval_s=15)


def assistant_text(text: str) -> Step:
    return lambda run, seq: AssistantText(v=2, run_id=run, seq=seq, text=text)


def heartbeat() -> Step:
    return lambda run, seq: Heartbeat(v=2, run_id=run, seq=seq)


def succeeded() -> Step:
    return lambda run, seq: Succeeded(v=2, run_id=run, seq=seq, text="fertig")


def failed(code: FailureCode) -> Step:
    return lambda run, seq: Failed(
        v=2, run_id=run, seq=seq, code=code, message=SERVICE_MESSAGE
    )


def cancelled() -> Step:
    return lambda run, seq: Cancelled(v=2, run_id=run, seq=seq)


def event_frames(*steps: Step) -> list[bytes]:
    run = uuid4()
    return [encode_event(step(run, seq)) for seq, step in enumerate(steps)]


def whole(frames: list[bytes]) -> list[bytes]:
    return [b"".join(frames)]


def byte_by_byte(frames: list[bytes]) -> list[bytes]:
    return [bytes([byte]) for byte in b"".join(frames)]


GOOD_ANSWER = (
    run_started(),
    assistant_text("Hallo "),
    heartbeat(),
    assistant_text("Welt."),
    succeeded(),
)


@dataclass
class Reply:
    """What the fake service answers one run with."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: dict(EVENT_STREAM))
    chunks: list[bytes] = field(default_factory=list[bytes])
    breaks_after_chunks: bool = False
    hangs_after_chunks: bool = False
    refuses_connection: bool = False
    late_chunks: list[bytes] = field(default_factory=list[bytes])
    gate: asyncio.Event | None = None


def reply_with(*chunks: bytes) -> Reply:
    return Reply(chunks=list(chunks))


def refusal_reply(code: RejectionCode, status: int, **headers: str) -> Reply:
    body = RunRejection(v=2, code=code, message=SERVICE_MESSAGE).model_dump_json()
    return Reply(
        status=status,
        headers={"content-type": "application/json", **headers},
        chunks=[body.encode()],
    )


class Chunks(httpx2.AsyncByteStream):
    """A response body that plays a reply's chunks and then breaks or hangs."""

    def __init__(self, reply: Reply) -> None:
        """Play this reply's chunks."""
        self._reply = reply
        self.closed = asyncio.Event()
        self.hung = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the chunks, then break or hang as the reply says."""
        for chunk in self._reply.chunks:
            yield chunk
        if self._reply.breaks_after_chunks:
            raise httpx2.ReadError(PERSON)
        if self._reply.hangs_after_chunks:
            self.hung.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


class Wire:
    """A MockTransport that records what reached it and answers with one reply."""

    def __init__(self, reply: Reply) -> None:
        """Answer every request with this reply."""
        self._reply = reply
        self.requests: list[httpx2.Request] = []
        self.transports_built = 0
        self.response_stream = Chunks(reply)

    def transport(self) -> httpx2.AsyncBaseTransport:
        self.transports_built += 1
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._reply.refuses_connection:
            raise httpx2.ConnectError(PERSON)
        return httpx2.Response(
            self._reply.status,
            headers=[
                (name.encode(), value.encode())
                for name, value in self._reply.headers.items()
            ],
            stream=self.response_stream,
        )


class RaisedBeforeAwaitingError(RuntimeError):
    """An error a token callable raises before it returns an awaitable."""


class CustomBaseError(BaseException):
    """A BaseException subclass of a supplier's own."""


class Token:
    """A token callable that counts its calls and may raise instead.

    A raised error carries a marker as its ``__context__``.
    """

    def __init__(self, value: str | BaseException) -> None:
        """Hand out this token, or raise this error."""
        self._value = value
        self.calls = 0

    def __call__(self) -> Awaitable[str]:
        self.calls += 1
        if isinstance(self._value, RaisedBeforeAwaitingError):
            self._raise(self._value)
        return self._read()

    async def _read(self) -> str:
        if isinstance(self._value, BaseException):
            self._raise(self._value)
        return self._value

    @staticmethod
    def _raise(error: BaseException) -> NoReturn:
        error.__context__ = RuntimeError(CONTEXT)
        raise error


@dataclass
class Outcome:
    """What one ask yielded and how it ended."""

    texts: list[str]
    error: CoPresenterUnavailableError | None


async def ask_once(
    answerer: ServiceAnswerer,
    person: Callable[[], Awaitable[str]],
    *,
    message: str,
) -> Outcome:
    texts: list[str] = []
    try:
        async with answerer.answer(message=message, person_token=person) as stream:
            while True:
                try:
                    texts.append(await anext(stream))
                except StopAsyncIteration:
                    break
    except CoPresenterUnavailableError as error:
        return Outcome(texts, error)
    return Outcome(texts, None)


@dataclass
class Asker:
    """One ask against a MockTransport: its token callables and its wire."""

    person: Token
    workload: Token
    wire: Wire
    answerer: ServiceAnswerer

    async def ask(self, *, message: str = QUESTION) -> Outcome:
        return await ask_once(self.answerer, self.person, message=message)


def an_asker(
    reply: Reply,
    *,
    person: str | BaseException = PERSON,
    workload: str | BaseException = WORKLOAD,
    origin: str = ORIGIN,
    monotonic: Callable[[], float] = time.monotonic,
) -> Asker:
    wire = Wire(reply)
    workload_token = Token(workload)
    return Asker(
        Token(person),
        workload_token,
        wire,
        ServiceAnswerer(
            origin=origin,
            workload_token=workload_token,
            transport=wire.transport,
            monotonic=monotonic,
        ),
    )


def unavailable(outcome: Outcome) -> CoPresenterUnavailableError:
    assert outcome.error is not None
    assert outcome.error.__cause__ is None
    assert outcome.error.__context__ is None
    return outcome.error


def warning_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


def summary_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.INFO
    ]


def one_warning(caplog: pytest.LogCaptureFixture) -> str:
    (warning,) = warning_lines(caplog)
    return warning


@dataclass(frozen=True)
class ReceivedCredentials:
    """The ``Authorization`` and ``Agent-Workload-Token`` headers of one request."""

    authorization: str | None
    workload_token: str | None


@asynccontextmanager
async def fake_service(
    reply_for: Callable[[str], Reply],
    *,
    credentials_seen: list[ReceivedCredentials] | None = None,
) -> AsyncGenerator[str]:
    """Serve ``POST /runs`` on 127.0.0.1; the reply may depend on the question.

    Each request's two credential headers are appended to ``credentials_seen``.
    """
    app = FastAPI()
    stopped = asyncio.Event()

    @app.post("/runs")
    async def runs(
        run: RunRequest,
        authorization: Annotated[str | None, Header()] = None,
        agent_workload_token: Annotated[str | None, Header()] = None,
    ) -> Response:
        if credentials_seen is not None:
            credentials_seen.append(
                ReceivedCredentials(authorization, agent_workload_token)
            )
        reply = reply_for(run.turn.messages[0].text)

        async def body() -> AsyncIterator[bytes]:
            for chunk in reply.chunks:
                yield chunk
            if reply.gate is not None:
                await reply.gate.wait()
            for chunk in reply.late_chunks:
                yield chunk
            if reply.breaks_after_chunks:
                raise RuntimeError
            if reply.hangs_after_chunks:
                await stopped.wait()

        return StreamingResponse(
            body(), status_code=reply.status, headers=reply.headers
        )

    listener = loopback_listener()
    port = listener.getsockname()[1]
    async with Serving(app, listener):
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            stopped.set()


def closed_port_origin() -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def violating_frame() -> bytes:
    return (
        b"id: 0\nevent: run_started\n"
        b'data: {"v": "violation-MARKER", "type": "run_started"}\n\n'
    )
