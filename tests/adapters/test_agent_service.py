"""The service answerer posts one run, reads it to its end and leaks nothing."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx2
import pytest
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
    TurnRole,
)
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.contracts.copresenter import CoPresenterUnavailableError
from tests.adapters.serving import Serving, loopback_listener

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from agent_client.wire import RunEvent

RUNS_URL = "http://agent-service.test/runs"
PERSON = "person-token-MARKER"
WORKLOAD = "workload-token-MARKER"
SYSTEM = "system-prompt-MARKER"
QUESTION = "question-MARKER"
SERVICE_MESSAGE = "service-message-MARKER"
VIOLATION_DETAIL = "violation-MARKER"
MARKERS = (PERSON, WORKLOAD, SYSTEM, QUESTION, SERVICE_MESSAGE, VIOLATION_DETAIL)
EVENT_STREAM = {"content-type": "text/event-stream"}
LOGGER = "presentator.adapters.agent_service"

type Step = Callable[[UUID, int], RunEvent]


def _started() -> Step:
    return lambda run, _seq: RunStarted(v=1, run_id=run, seq=0, heartbeat_interval_s=15)


def _text(text: str) -> Step:
    return lambda run, seq: AssistantText(v=1, run_id=run, seq=seq, text=text)


def _beat() -> Step:
    return lambda run, seq: Heartbeat(v=1, run_id=run, seq=seq)


def _succeeded() -> Step:
    return lambda run, seq: Succeeded(v=1, run_id=run, seq=seq, text="fertig")


def _failed(code: FailureCode) -> Step:
    return lambda run, seq: Failed(
        v=1, run_id=run, seq=seq, code=code, message=SERVICE_MESSAGE
    )


def _cancelled() -> Step:
    return lambda run, seq: Cancelled(v=1, run_id=run, seq=seq)


def _frames(*steps: Step) -> list[bytes]:
    run = uuid4()
    return [encode_event(step(run, seq)) for seq, step in enumerate(steps)]


def _whole(frames: list[bytes]) -> list[bytes]:
    return [b"".join(frames)]


def _byte_by_byte(frames: list[bytes]) -> list[bytes]:
    return [bytes([byte]) for byte in b"".join(frames)]


_GOOD_ANSWER = (_started(), _text("Hallo "), _beat(), _text("Welt."), _succeeded())


@dataclass
class _Reply:
    """What the fake service answers one run with."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: dict(EVENT_STREAM))
    chunks: list[bytes] = field(default_factory=list[bytes])
    breaks_after_chunks: bool = False
    hangs_after_chunks: bool = False
    refuses_connection: bool = False
    late_chunks: list[bytes] = field(default_factory=list[bytes])
    gate: asyncio.Event | None = None


def _answer(*chunks: bytes) -> _Reply:
    return _Reply(chunks=list(chunks))


def _refusal(code: RejectionCode, status: int, **headers: str) -> _Reply:
    body = RunRejection(v=1, code=code, message="Dienst sagt nein").model_dump_json()
    return _Reply(
        status=status,
        headers={"content-type": "application/json", **headers},
        chunks=[body.encode()],
    )


class _Chunks(httpx2.AsyncByteStream):
    def __init__(self, reply: _Reply) -> None:
        self._reply = reply
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._reply.chunks:
            yield chunk
        if self._reply.breaks_after_chunks:
            raise httpx2.ReadError(PERSON)
        if self._reply.hangs_after_chunks:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


class _Wire:
    """A MockTransport that records what reached it and answers with one reply."""

    def __init__(self, reply: _Reply) -> None:
        self._reply = reply
        self.requests: list[httpx2.Request] = []
        self.transports_built = 0
        self.response_stream = _Chunks(reply)

    def transport(self) -> httpx2.AsyncBaseTransport:
        self.transports_built += 1
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._reply.refuses_connection:
            raise httpx2.ConnectError(PERSON)
        return httpx2.Response(
            self._reply.status,
            headers=self._reply.headers,
            stream=self.response_stream,
        )


class _Token:
    """A token callable that counts its calls and may raise instead."""

    def __init__(self, value: str | Exception) -> None:
        self._value = value
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


@dataclass
class _Outcome:
    texts: list[str]
    error: CoPresenterUnavailableError | None


async def _ask(
    answerer: ServiceAnswerer, person: _Token, *, system: str, message: str
) -> _Outcome:
    texts: list[str] = []
    try:
        async with answerer.answer(
            system=system, message=message, person_token=person
        ) as stream:
            while True:
                try:
                    texts.append(await anext(stream))
                except StopAsyncIteration:
                    break
    except CoPresenterUnavailableError as error:
        return _Outcome(texts, error)
    return _Outcome(texts, None)


@dataclass
class _Asker:
    """One ask against a MockTransport: its token callables and its wire."""

    person: _Token
    workload: _Token
    wire: _Wire
    answerer: ServiceAnswerer

    async def ask(self, *, system: str = SYSTEM, message: str = QUESTION) -> _Outcome:
        return await _ask(self.answerer, self.person, system=system, message=message)


def _asker(
    reply: _Reply,
    *,
    person: str | Exception = PERSON,
    workload: str | Exception = WORKLOAD,
) -> _Asker:
    wire = _Wire(reply)
    workload_token = _Token(workload)
    return _Asker(
        _Token(person),
        workload_token,
        wire,
        ServiceAnswerer(
            runs_url=RUNS_URL, workload_token=workload_token, transport=wire.transport
        ),
    )


def _unavailable(outcome: _Outcome) -> CoPresenterUnavailableError:
    assert outcome.error is not None
    assert outcome.error.__cause__ is None
    assert outcome.error.__context__ is None
    return outcome.error


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


def _one_warning(caplog: pytest.LogCaptureFixture) -> str:
    (warning,) = _warnings(caplog)
    return warning


def test_the_request_is_the_run_request_of_the_contract() -> None:
    asker = _asker(_answer(*_whole(_frames(*_GOOD_ANSWER))))

    asyncio.run(asker.ask())

    (request,) = asker.wire.requests
    assert request.method == "POST"
    assert str(request.url) == RUNS_URL
    run = RunRequest.model_validate_json(request.content)
    assert (run.v, run.purpose) == (1, "copresenter")
    assert run.turn.system == SYSTEM
    assert [(m.role, m.text) for m in run.turn.messages] == [(TurnRole.USER, QUESTION)]
    assert request.extensions["timeout"] == {
        "connect": 5,
        "write": 5,
        "pool": 5,
        "read": 60,
    }


def test_both_tokens_go_with_every_run_and_each_is_awaited_once() -> None:
    asker = _asker(_answer(*_whole(_frames(*_GOOD_ANSWER))))

    asyncio.run(asker.ask())

    (request,) = asker.wire.requests
    assert request.headers["authorization"] == f"Bearer {PERSON}"
    assert request.headers["agent-workload-token"] == WORKLOAD
    assert request.headers["accept"] == "text/event-stream"
    assert (asker.person.calls, asker.workload.calls) == (1, 1)


@pytest.mark.parametrize("split", [_whole, _byte_by_byte], ids=["whole", "bytes"])
def test_the_answer_is_the_assistant_text(
    split: Callable[[list[bytes]], list[bytes]],
) -> None:
    asker = _asker(_answer(*split(_frames(*_GOOD_ANSWER))))

    outcome = asyncio.run(asker.ask())

    assert outcome == _Outcome(["Hallo ", "Welt."], None)


def _extra_in_the_terminals_chunk(frames: list[bytes]) -> list[bytes]:
    return _whole(frames)


def _extra_in_a_later_chunk(frames: list[bytes]) -> list[bytes]:
    return [*_whole(frames[:3]), frames[3]]


@pytest.mark.parametrize(
    "arrange",
    [_extra_in_the_terminals_chunk, _extra_in_a_later_chunk],
    ids=["same-chunk", "later-chunk"],
)
def test_nothing_may_follow_the_terminal(
    arrange: Callable[[list[bytes]], list[bytes]], caplog: pytest.LogCaptureFixture
) -> None:
    frames = _frames(_started(), _text("Hallo"), _succeeded(), _text("zu viel"))
    asker = _asker(_answer(*arrange(frames)))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    _unavailable(outcome)
    assert "event_after_terminal" in _one_warning(caplog)


@pytest.mark.parametrize(
    "code", [code for code in FailureCode if code is not FailureCode.INTERRUPTED]
)
def test_a_failed_run_ends_as_unavailable_and_names_its_code(
    code: FailureCode, caplog: pytest.LogCaptureFixture
) -> None:
    asker = _asker(_answer(*_frames(_started(), _text("Hallo"), _failed(code))))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    _unavailable(outcome)
    assert code.value in _one_warning(caplog)


@pytest.mark.parametrize(
    ("ending", "named"),
    [
        (_failed(FailureCode.INTERRUPTED), "interrupted_without_side_effect"),
        (_cancelled(), "cancelled"),
    ],
    ids=["interrupted", "cancelled"],
)
def test_an_interrupted_or_cancelled_run_ends_as_unavailable(
    ending: Step, named: str, caplog: pytest.LogCaptureFixture
) -> None:
    asker = _asker(_answer(*_frames(_started(), _text("Hallo"), ending)))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    _unavailable(outcome)
    assert named in _one_warning(caplog)


def test_a_stream_without_a_terminal_event_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    asker = _asker(_answer(*_frames(_started(), _text("Hallo"))))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    _unavailable(outcome)
    assert "truncated" in _one_warning(caplog)


def test_a_sequence_gap_is_unavailable_and_nothing_after_it_is_yielded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frames = _frames(_started(), _text("Hallo"), _text("fehlt"), _text("danach"))
    asker = _asker(_answer(*_whole([frames[0], frames[1], frames[3]])))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    _unavailable(outcome)
    assert "sequence_gap" in _one_warning(caplog)


_SERVICE_STATUS = {
    RejectionCode.NOT_ENABLED: 403,
    RejectionCode.FORBIDDEN: 403,
    RejectionCode.BUSY: 429,
    RejectionCode.UNAUTHORIZED: 401,
    RejectionCode.UNAVAILABLE: 503,
    RejectionCode.UNSUPPORTED_VERSION: 400,
    RejectionCode.INVALID_REQUEST: 422,
    RejectionCode.CAPABILITY_TOO_LARGE: 422,
    RejectionCode.NOT_FOUND: 404,
}
_SERVICE_RETRY_AFTER = {RejectionCode.BUSY: "10", RejectionCode.UNAVAILABLE: "30"}


@pytest.mark.parametrize("code", list(RejectionCode))
def test_every_rejection_is_unavailable_and_logs_its_code(
    code: RejectionCode, caplog: pytest.LogCaptureFixture
) -> None:
    retry_after = _SERVICE_RETRY_AFTER.get(code)
    headers = {} if retry_after is None else {"retry-after": retry_after}
    asker = _asker(_refusal(code, _SERVICE_STATUS[code], **headers))

    outcome = asyncio.run(asker.ask())

    _unavailable(outcome)
    warning = _one_warning(caplog)
    assert code.value in warning
    assert (retry_after or "None") in warning


_UNKNOWN_CODE = b'{"v": 1, "code": "bribe_me", "message": "Dienst sagt nein"}'


@pytest.mark.parametrize(
    "reply",
    [
        _Reply(
            status=403,
            headers={"content-type": "application/json"},
            chunks=[_UNKNOWN_CODE],
        ),
        _Reply(
            status=502, headers={"content-type": "text/html"}, chunks=[b"<html></html>"]
        ),
        _Reply(
            status=200,
            headers={"content-type": "application/json"},
            chunks=[b"{}"],
        ),
    ],
    ids=["unknown-code", "html-502", "json-200"],
)
def test_a_refusal_without_a_known_code_is_unavailable_with_code_none(
    reply: _Reply, caplog: pytest.LogCaptureFixture
) -> None:
    asker = _asker(reply)

    outcome = asyncio.run(asker.ask())

    _unavailable(outcome)
    assert "None" in _one_warning(caplog)
    assert "bribe_me" not in _one_warning(caplog)


@pytest.mark.parametrize("retry_after", ["soon", "-5", "1e3"])
def test_a_retry_after_that_is_not_a_whole_number_of_seconds_is_not_logged(
    retry_after: str, caplog: pytest.LogCaptureFixture
) -> None:
    asker = _asker(_refusal(RejectionCode.BUSY, 429, **{"retry-after": retry_after}))

    asyncio.run(asker.ask())

    warning = _one_warning(caplog)
    assert "retry_after=None" in warning
    assert retry_after not in warning


@pytest.mark.parametrize(
    ("person", "workload", "system", "message"),
    [
        (RuntimeError(PERSON), WORKLOAD, SYSTEM, QUESTION),
        (PERSON, RuntimeError(WORKLOAD), SYSTEM, QUESTION),
        ("bad\r\ntoken", WORKLOAD, SYSTEM, QUESTION),
        (PERSON, "bad token", SYSTEM, QUESTION),
        (PERSON, "tökén", SYSTEM, QUESTION),
        ("", WORKLOAD, SYSTEM, QUESTION),
        (PERSON, WORKLOAD, "\ud800", QUESTION),
        (PERSON, WORKLOAD, SYSTEM, "\ud800"),
    ],
    ids=[
        "person-raises",
        "workload-raises",
        "person-crlf",
        "workload-space",
        "workload-non-ascii",
        "person-empty",
        "system-surrogate",
        "message-surrogate",
    ],
)
def test_a_failure_before_the_run_sends_no_run(
    person: str | Exception,
    workload: str | Exception,
    system: str,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    asker = _asker(
        _answer(*_whole(_frames(*_GOOD_ANSWER))), person=person, workload=workload
    )

    outcome = asyncio.run(asker.ask(system=system, message=message))

    _unavailable(outcome)
    assert outcome.texts == []
    assert asker.wire.transports_built == 0
    assert asker.wire.requests == []
    _one_warning(caplog)


def test_a_transport_that_cannot_connect_is_unavailable() -> None:
    asker = _asker(_Reply(refuses_connection=True))

    outcome = asyncio.run(asker.ask())

    _unavailable(outcome)
    assert asker.wire.transports_built == 1


def test_a_transport_failure_mid_stream_is_unavailable_after_the_text() -> None:
    asker = _asker(
        _Reply(
            chunks=_whole(_frames(_started(), _text("Hallo"))), breaks_after_chunks=True
        )
    )

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    _unavailable(outcome)


def test_leaving_early_closes_the_response() -> None:
    asker = _asker(
        _Reply(
            chunks=_whole(_frames(_started(), _text("Hallo"))), hangs_after_chunks=True
        )
    )

    async def leave_after_first_text() -> None:
        async with asker.answerer.answer(
            system=SYSTEM, message=QUESTION, person_token=asker.person
        ) as stream:
            assert await anext(stream) == "Hallo"
        assert asker.wire.response_stream.closed.is_set()

    asyncio.run(leave_after_first_text())


@asynccontextmanager
async def _fake_service(reply_for: Callable[[str], _Reply]) -> AsyncGenerator[str]:
    """Serve ``POST /runs`` on 127.0.0.1; the reply may depend on the question."""
    app = FastAPI()
    stopped = asyncio.Event()

    @app.post("/runs")
    async def runs(run: RunRequest) -> Response:
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
            yield f"http://127.0.0.1:{port}/runs"
        finally:
            stopped.set()


def _closed_port_url() -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}/runs"


def test_one_asks_end_leaves_another_intact() -> None:
    first_left = asyncio.Event()
    second_frames = _frames(
        _started(), _text("zweite "), _text("Antwort."), _succeeded()
    )

    def reply_for(question: str) -> _Reply:
        if question == "first":
            return _Reply(
                chunks=_whole(_frames(_started(), _text("erst"))),
                hangs_after_chunks=True,
            )
        return _Reply(
            chunks=_whole(second_frames[:2]),
            gate=first_left,
            late_chunks=_whole(second_frames[2:]),
        )

    async def drive() -> list[str]:
        async with _fake_service(reply_for) as url:
            answerer = ServiceAnswerer(runs_url=url, workload_token=_Token(WORKLOAD))
            person = _Token(PERSON)
            async with answerer.answer(
                system=SYSTEM, message="second", person_token=person
            ) as second:
                async with answerer.answer(
                    system=SYSTEM, message="first", person_token=person
                ) as first:
                    assert await anext(first) == "erst"
                first_left.set()
                return [text async for text in second]

    assert asyncio.run(drive()) == ["zweite ", "Antwort."]


def _violating_frame() -> bytes:
    return (
        b"id: 0\nevent: run_started\n"
        b'data: {"v": "violation-MARKER", "type": "run_started"}\n\n'
    )


@dataclass(frozen=True)
class _Leak:
    """One way an ask can go, with the arguments that drive it."""

    reply: _Reply
    person: str | Exception = PERSON
    workload: str | Exception = WORKLOAD
    message: str = QUESTION


_LEAK_CASES = {
    "success": _Leak(_answer(*_whole(_frames(*_GOOD_ANSWER)))),
    "refusal": _Leak(_refusal(RejectionCode.BUSY, 429, **{"retry-after": "10"})),
    "person-raises": _Leak(_answer(), person=RuntimeError(PERSON)),
    "workload-raises": _Leak(_answer(), workload=RuntimeError(WORKLOAD)),
    "person-crlf": _Leak(_answer(), person=f"{PERSON}\r\n"),
    "workload-space": _Leak(_answer(), workload=f"{WORKLOAD} x"),
    "workload-non-ascii": _Leak(_answer(), workload=f"{WORKLOAD}é"),
    "surrogate": _Leak(_answer(), message=f"{QUESTION}\ud800"),
    "failed-message": _Leak(
        _answer(*_frames(_started(), _failed(FailureCode.PROVIDER_ERROR)))
    ),
    "violation-detail": _Leak(_answer(_violating_frame())),
    "connection-dropped": _Leak(
        _Reply(
            chunks=_whole(_frames(_started(), _text("Hallo"))),
            breaks_after_chunks=True,
        )
    ),
    "cannot-connect": _Leak(_Reply(refuses_connection=True)),
}


@pytest.mark.parametrize("transport", ["mock", "fake-service"])
@pytest.mark.parametrize("case", list(_LEAK_CASES))
def test_no_secret_and_no_question_leaks(
    case: str, transport: str, caplog: pytest.LogCaptureFixture
) -> None:
    leak = _LEAK_CASES[case]
    reply, person, workload, message = (
        leak.reply,
        leak.person,
        leak.workload,
        leak.message,
    )
    caplog.set_level(logging.DEBUG)

    async def drive() -> _Outcome:
        if transport == "mock":
            return await _asker(reply, person=person, workload=workload).ask(
                message=message
            )
        async with _fake_service(lambda _question: reply) as url:
            runs_url = _closed_port_url() if reply.refuses_connection else url
            answerer = ServiceAnswerer(
                runs_url=runs_url, workload_token=_Token(workload)
            )
            return await _ask(answerer, _Token(person), system=SYSTEM, message=message)

    outcome = asyncio.run(drive())

    for record in caplog.records:
        shown = f"{record.getMessage()} {record.args} {record.exc_text}"
        assert record.exc_info is None
        assert not [marker for marker in MARKERS if marker in shown]
    if outcome.error is not None:
        error = _unavailable(outcome)
        assert not [m for m in MARKERS if m in f"{error!s} {error!r}"]
