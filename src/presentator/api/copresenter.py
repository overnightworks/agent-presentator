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
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from presentator.contracts.copresenter import (
    AnswerEvent,
    AnswerText,
    AnswerUnavailable,
    Audio,
    CoPresenterUnavailableError,
    Done,
    HearingUnavailable,
    Question,
    Sentence,
)

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import Message

    from presentator.application.copresenter import CoPresenterHearing, CoPresenterUse

_CHECK_INTERVAL_SECONDS: Final = 0.5
_POLICY_VIOLATION: Final = 1008
_UNSUPPORTED_DATA: Final = 1003
_INTERNAL_ERROR: Final = 1011
_GENERIC_ANSWER_FAILURE: Final = "the answer could not be spoken"
_GENERIC_HEARING_FAILURE: Final = "hearing unavailable"
_BROWSER_DISCONNECT: Final = "websocket.disconnect"


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
        admitted = self.admission(connection)
        return admitted is not None and admitted.session_id == session_id

    async def wait(self) -> None:
        await self.delay(_CHECK_INTERVAL_SECONDS)


class AskRequest(BaseModel, extra="forbid"):
    """The only browser fields a private answer may receive."""

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
        except CoPresenterUnavailableError:
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

    def _ask(request: Request, question: AskRequest) -> Response:
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
        connection = admission(socket)
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
    session_watch = asyncio.create_task(
        _watch_session(request, session_id=session_id, guard=guard)
    )
    try:
        try:
            async with use.answer(question) as events:
                next_event = asyncio.create_task(_next_answer_event(events))
                try:
                    while True:
                        done, _pending = await asyncio.wait(
                            {next_event, session_watch},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if session_watch in done:
                            _observe_session_end(session_watch)
                            return
                        try:
                            event = next_event.result()
                        except StopAsyncIteration:
                            return
                        yield _sse(event)
                        if isinstance(event, (Done, AnswerUnavailable)):
                            return
                        next_event = asyncio.create_task(_next_answer_event(events))
                finally:
                    await _finish_tasks((next_event,))
        except CoPresenterUnavailableError:
            yield _error_sse()
    finally:
        await _finish_tasks((session_watch,))


async def _hearing_lease(
    socket: WebSocket,
    *,
    language: str,
    session_id: str,
    use: CoPresenterUse,
    guard: _SessionGuard,
) -> None:
    session_watch = asyncio.create_task(
        _watch_session(socket, session_id=session_id, guard=guard)
    )
    try:
        first_frame = await _next_pcm_or_end(
            socket,
            session_watch=session_watch,
        )
        if first_frame is None:
            return
        try:
            async with use.hear(language) as hearing:
                await hearing.send_pcm(first_frame)
                private_ended = await _pipe_hearing(
                    socket,
                    hearing=hearing,
                    session_watch=session_watch,
                )
        except CoPresenterUnavailableError:
            private_ended = True
        if private_ended:
            await socket.send_json(
                {"text": "", "final": True, "error": _GENERIC_HEARING_FAILURE},
            )
            await _hold_authenticated_lease(
                socket,
                session_watch=session_watch,
            )
    except WebSocketDisconnect:
        return
    except RuntimeError:
        with suppress(RuntimeError):
            await socket.close(code=_INTERNAL_ERROR)
    finally:
        await _finish_tasks((session_watch,))


async def _next_pcm_or_end(
    socket: WebSocket,
    *,
    session_watch: asyncio.Task[None],
) -> bytes | None:
    receive = asyncio.create_task(socket.receive())
    try:
        done, _pending = await asyncio.wait(
            {receive, session_watch}, return_when=asyncio.FIRST_COMPLETED
        )
        if session_watch in done:
            _observe_session_end(session_watch)
            await socket.close(code=_POLICY_VIOLATION)
            return None
        return await _pcm_or_end(socket, receive.result())
    finally:
        await _finish_tasks((receive,))


async def _pcm_or_end(socket: WebSocket, message: Message) -> bytes | None:
    if message["type"] == _BROWSER_DISCONNECT:
        return None
    if message.get("text") is not None:
        await socket.close(code=_UNSUPPORTED_DATA)
        return None
    return message["bytes"]


async def _pipe_hearing(
    socket: WebSocket,
    *,
    hearing: CoPresenterHearing,
    session_watch: asyncio.Task[None],
) -> bool:
    receive = asyncio.create_task(socket.receive())
    transcript = asyncio.create_task(hearing.receive())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive, transcript, session_watch},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if session_watch in done:
                _observe_session_end(session_watch)
                await socket.close(code=_POLICY_VIOLATION)
                return False
            if receive in done:
                frame = await _pcm_or_end(socket, receive.result())
                if frame is None:
                    return False
                await hearing.send_pcm(frame)
                receive = asyncio.create_task(socket.receive())
            if transcript in done:
                event = transcript.result()
                if event is None or isinstance(event, HearingUnavailable):
                    return True
                await socket.send_json({"text": event.text, "final": event.final})
                transcript = asyncio.create_task(hearing.receive())
    except WebSocketDisconnect:
        return False
    finally:
        await _finish_tasks((receive, transcript))


async def _hold_authenticated_lease(
    socket: WebSocket,
    *,
    session_watch: asyncio.Task[None],
) -> None:
    receive = asyncio.create_task(socket.receive())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive, session_watch}, return_when=asyncio.FIRST_COMPLETED
            )
            if session_watch in done:
                _observe_session_end(session_watch)
                await socket.close(code=_POLICY_VIOLATION)
                return
            message = receive.result()
            if message["type"] == _BROWSER_DISCONNECT:
                return
            if message.get("text") is not None:
                await socket.close(code=_UNSUPPORTED_DATA)
                return
            receive = asyncio.create_task(socket.receive())
    finally:
        await _finish_tasks((receive,))


async def _watch_session(
    connection: HTTPConnection,
    *,
    session_id: str,
    guard: _SessionGuard,
) -> None:
    while True:
        await guard.wait()
        if not guard.same_session(connection, session_id):
            return


def _observe_session_end(session_watch: asyncio.Task[None]) -> None:
    session_watch.exception()


async def _next_answer_event(events: AsyncIterator[AnswerEvent]) -> AnswerEvent:
    return await anext(events)


async def _finish_tasks(tasks: Iterable[asyncio.Task[object]]) -> None:
    owned = tuple(tasks)
    for task in owned:
        if not task.done():
            task.cancel()
    await asyncio.wait(owned)
    for task in owned:
        if not task.cancelled():
            task.exception()


def _sse(event: AnswerEvent) -> bytes:
    if isinstance(event, AnswerText):
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
