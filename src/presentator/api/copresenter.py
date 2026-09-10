"""Authenticated browser routes for the private co-presenter."""

from __future__ import annotations

import asyncio
import json
from base64 import b64encode
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, Protocol

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import HTTPConnection

from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerUnavailable,
    Audio,
    CoPresenterUnavailable,
    Done,
    HearingUnavailable,
    Question,
    Sentence,
    Text,
)

if TYPE_CHECKING:
    from starlette.responses import Response

    from presentator.application.copresenter import CoPresenterHearing, CoPresenterUse

_CHECK_INTERVAL_SECONDS: Final = 1.0
_POLICY_VIOLATION: Final = 1008
_UNSUPPORTED_DATA: Final = 1003
_INTERNAL_ERROR: Final = 1011
_GENERIC_ANSWER_FAILURE: Final = "the answer could not be spoken"
_GENERIC_HEARING_FAILURE: Final = "hearing unavailable"


class SignedIn(Protocol):
    """The admission fact operation watchers retain."""

    @property
    def session_id(self) -> str:
        """The authoritative session id retained by an operation."""
        ...


Admission = Callable[[HTTPConnection], SignedIn | None]
Delay = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class CoPresenterSurface:
    """The use case and browser origin installed on the public surface."""

    use: CoPresenterUse
    public_origin: str
    delay: Delay = asyncio.sleep


@dataclass(frozen=True, slots=True, kw_only=True)
class _SessionGuard:
    admission: Admission
    delay: Delay

    def same_session(self, connection: HTTPConnection, session_id: str) -> bool:
        admitted = self.admit(connection)
        return admitted is not None and admitted.session_id == session_id

    def admit(self, connection: HTTPConnection) -> SignedIn | None:
        try:
            return self.admission(connection)
        except Exception:
            return None

    async def wait(self) -> None:
        await self.delay(_CHECK_INTERVAL_SECONDS)


class AskRequest(BaseModel):
    """The only browser fields a private answer may receive."""

    model_config = ConfigDict(extra="forbid")

    said: str = Field(min_length=1)
    slide: int = Field(ge=1)
    language: str | None = None


def add_copresenter_routes(
    app: FastAPI,
    *,
    surface: CoPresenterSurface,
    admission: Admission,
) -> None:
    """Add the exact signed-in co-presenter allowlist to the lobby."""
    router = APIRouter(prefix="/copresenter")
    use = surface.use
    guard = _SessionGuard(admission=admission, delay=surface.delay)

    async def _who(request: Request) -> JSONResponse:
        if admission(request) is None:
            return JSONResponse(
                {"detail": "Not authenticated"}, HTTPStatus.UNAUTHORIZED
            )
        try:
            ready = await use.readiness()
        except CoPresenterUnavailable:
            return JSONResponse(
                {"detail": "Co-presenter unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE
            )
        return JSONResponse(
            {
                "answerer": {"model": ready.answerer_model},
                "speech": {
                    "sample_rate": ready.hearing_sample_rate,
                    "hearing": {"ready": ready.local_hearing_ready},
                },
            },
        )

    async def _ask(request: Request, question: AskRequest) -> Response:
        connection = admission(request)
        if connection is None:
            return JSONResponse(
                {"detail": "Not authenticated"}, HTTPStatus.UNAUTHORIZED
            )
        return StreamingResponse(
            _answer_events(
                request,
                question=Question(
                    said=question.said,
                    slide=question.slide,
                    language=question.language,
                ),
                session_id=connection.session_id,
                use=use,
                guard=guard,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _hear(socket: WebSocket, language: str = "de") -> None:
        if (
            socket.headers.get("origin") != surface.public_origin
            or any(key != "language" for key in socket.query_params)
            or len(socket.query_params.getlist("language")) > 1
        ):
            await socket.close(code=_POLICY_VIOLATION)
            return
        connection = guard.admit(socket)
        if connection is None:
            await socket.close(code=_POLICY_VIOLATION)
            return
        await socket.accept()
        await _hearing_lease(
            socket,
            language=language,
            session_id=connection.session_id,
            use=use,
            guard=guard,
        )

    router.add_api_route("/who", _who, methods=["GET"])
    router.add_api_route("/ask", _ask, methods=["POST"])
    router.add_api_websocket_route("/hear", _hear)
    app.include_router(router)


async def _answer_events(
    request: Request,
    *,
    question: Question,
    session_id: str,
    use: CoPresenterUse,
    guard: _SessionGuard,
) -> AsyncIterator[bytes]:
    next_event: asyncio.Task[AnswerEvent] | None = None
    recheck: asyncio.Task[None] | None = None
    try:
        async with use.answer(question) as events:
            next_event = asyncio.create_task(_next_answer_event(events))
            while True:
                recheck = asyncio.create_task(guard.wait())
                done, _pending = await asyncio.wait(
                    {next_event, recheck},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if recheck in done:
                    if not guard.same_session(request, session_id):
                        return
                    recheck = None
                    if next_event not in done:
                        continue
                else:
                    recheck.cancel()
                    await _await_cancelled(recheck)
                recheck = None
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                yield _sse(event)
                if isinstance(event, (Done, AnswerUnavailable)):
                    return
                next_event = asyncio.create_task(_next_answer_event(events))
    except CoPresenterUnavailable:
        yield _error_sse()
    finally:
        for task in (next_event, recheck):
            if task is not None and not task.done():
                task.cancel()
                await _await_cancelled(task)


async def _hearing_lease(
    socket: WebSocket,
    *,
    language: str,
    session_id: str,
    use: CoPresenterUse,
    guard: _SessionGuard,
) -> None:
    try:
        first_frame = await _next_pcm_or_end(
            socket,
            session_id=session_id,
            guard=guard,
        )
        if first_frame is None:
            return
        try:
            async with use.hear(language) as hearing:
                await hearing.send_pcm(first_frame)
                private_ended = await _pipe_hearing(
                    socket,
                    hearing=hearing,
                    session_id=session_id,
                    guard=guard,
                )
        except CoPresenterUnavailable:
            private_ended = True
        if private_ended:
            await socket.send_json(
                {"text": "", "final": True, "error": _GENERIC_HEARING_FAILURE},
            )
            await _hold_authenticated_lease(
                socket,
                session_id=session_id,
                guard=guard,
            )
    except WebSocketDisconnect:
        return
    except RuntimeError:
        with suppress(RuntimeError):
            await socket.close(code=_INTERNAL_ERROR)


async def _next_pcm_or_end(
    socket: WebSocket,
    *,
    session_id: str,
    guard: _SessionGuard,
) -> bytes | None:
    while True:
        receive = asyncio.create_task(socket.receive())
        recheck = asyncio.create_task(guard.wait())
        done, _pending = await asyncio.wait(
            {receive, recheck}, return_when=asyncio.FIRST_COMPLETED
        )
        if recheck in done:
            if not guard.same_session(socket, session_id):
                receive.cancel()
                await _await_cancelled(receive)
                await socket.close(code=_POLICY_VIOLATION)
                return None
            if receive not in done:
                receive.cancel()
                await _await_cancelled(receive)
                continue
        else:
            recheck.cancel()
            await _await_cancelled(recheck)
        message = receive.result()
        if message["type"] == "websocket.disconnect":
            return None
        if message.get("text") is not None:
            await socket.close(code=_UNSUPPORTED_DATA)
            return None
        frame = message.get("bytes")
        if frame is not None:
            return frame


async def _pipe_hearing(
    socket: WebSocket,
    *,
    hearing: CoPresenterHearing,
    session_id: str,
    guard: _SessionGuard,
) -> bool:
    while True:
        receive = asyncio.create_task(socket.receive())
        transcript = asyncio.create_task(hearing.receive())
        recheck = asyncio.create_task(guard.wait())
        done, pending = await asyncio.wait(
            {receive, transcript, recheck},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if recheck in done and not guard.same_session(socket, session_id):
            await _cancel_all(pending)
            await socket.close(code=_POLICY_VIOLATION)
            return False
        if receive in done:
            message = receive.result()
            if message["type"] == "websocket.disconnect":
                await _cancel_all(pending)
                return False
            if message.get("text") is not None:
                await _cancel_all(pending)
                await socket.close(code=_UNSUPPORTED_DATA)
                return False
            frame = message.get("bytes")
            if frame is not None:
                await hearing.send_pcm(frame)
        if transcript in done:
            event = transcript.result()
            if event is None or isinstance(event, HearingUnavailable):
                await _cancel_all(pending)
                return True
            await socket.send_json({"text": event.text, "final": event.final})
        await _cancel_all(pending)


async def _hold_authenticated_lease(
    socket: WebSocket,
    *,
    session_id: str,
    guard: _SessionGuard,
) -> None:
    while True:
        message = await _next_pcm_or_end(
            socket,
            session_id=session_id,
            guard=guard,
        )
        if message is None:
            return


async def _next_answer_event(events: AsyncIterator[AnswerEvent]) -> AnswerEvent:
    return await anext(events)


async def _cancel_all(tasks: Iterable[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        await _await_cancelled(task)


async def _await_cancelled(task: asyncio.Task[object]) -> None:
    with suppress(asyncio.CancelledError):
        await task


def _sse(event: AnswerEvent) -> bytes:
    if isinstance(event, Text):
        return _encoded_sse("text", {"text": event.text})
    if isinstance(event, Sentence):
        return _encoded_sse("sentence", {"text": event.text})
    if isinstance(event, Audio):
        return _encoded_sse(
            "audio",
            {"text": event.text, "wav_b64": b64encode(event.wav).decode("ascii")},
        )
    if isinstance(event, Done):
        return _encoded_sse("done", {"text": event.text})
    return _error_sse()


def _error_sse() -> bytes:
    return _encoded_sse("error", {"message": _GENERIC_ANSWER_FAILURE})


def _encoded_sse(event: str, payload: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
