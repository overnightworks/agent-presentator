"""The service answerer handles its two tokens without leaking them or a chain."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from agent_client.wire import (
    FailureCode,
    RejectionCode,
    RunRejection,
)

from presentator.adapters.agent_service import ServiceAnswerer
from tests.adapters.agent_service_support import (
    GOOD_ANSWER,
    MARKERS,
    ORIGIN,
    PERSON,
    QUESTION,
    WORKLOAD,
    CustomBaseError,
    Outcome,
    RaisedBeforeAwaitingError,
    Reply,
    Token,
    Wire,
    an_asker,
    ask_once,
    assistant_text,
    closed_port_origin,
    event_frames,
    failed,
    fake_service,
    one_warning,
    refusal_reply,
    reply_with,
    run_started,
    unavailable,
    violating_frame,
    whole,
)
from tests.leak_support import everything_shown

if TYPE_CHECKING:
    from collections.abc import Awaitable


@pytest.mark.parametrize(
    ("person", "workload", "message"),
    [
        (RuntimeError(PERSON), WORKLOAD, QUESTION),
        (PERSON, RuntimeError(WORKLOAD), QUESTION),
        (RaisedBeforeAwaitingError(PERSON), WORKLOAD, QUESTION),
        (PERSON, RaisedBeforeAwaitingError(WORKLOAD), QUESTION),
        ("bad\r\ntoken", WORKLOAD, QUESTION),
        (PERSON, "bad token", QUESTION),
        (PERSON, "tökén", QUESTION),
        ("", WORKLOAD, QUESTION),
        (PERSON, WORKLOAD, "\ud800"),
    ],
    ids=[
        "person-raises",
        "workload-raises",
        "person-raises-before-awaiting",
        "workload-raises-before-awaiting",
        "person-crlf",
        "workload-space",
        "workload-non-ascii",
        "person-empty",
        "message-surrogate",
    ],
)
def test_a_failure_before_the_run_sends_no_run(
    person: str | BaseException,
    workload: str | BaseException,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    asker = an_asker(
        reply_with(*whole(event_frames(*GOOD_ANSWER))), person=person, workload=workload
    )

    outcome = asyncio.run(asker.ask(message=message))

    unavailable(outcome)
    assert outcome.texts == []
    assert asker.wire.transports_built == 0
    assert asker.wire.requests == []
    one_warning(caplog)


@pytest.mark.parametrize("which", ["person", "workload"])
def test_a_token_callable_that_is_cancelled_propagates_the_cancellation(
    which: str,
) -> None:
    cancelled = asyncio.CancelledError()
    asker = an_asker(
        reply_with(*whole(event_frames(*GOOD_ANSWER))),
        person=cancelled if which == "person" else PERSON,
        workload=cancelled if which == "workload" else WORKLOAD,
    )
    run = asker.ask()

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(run)

    assert asker.wire.requests == []
    assert caught.value is cancelled
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("which", ["person", "workload"])
@pytest.mark.parametrize("when", ["before-awaiting", "after-awaiting"])
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit(1)], ids=["keyboard", "exit"]
)
def test_a_token_callable_that_interrupts_leaves_answer_to_the_caller(
    which: str, when: str, error: BaseException
) -> None:
    async def read() -> str:
        raise error

    def interrupting() -> Awaitable[str]:
        if when == "before-awaiting":
            raise error
        return read()

    async def drive() -> BaseException:
        wire = Wire(reply_with())
        answerer = ServiceAnswerer(
            origin=ORIGIN,
            workload_token=interrupting if which == "workload" else Token(WORKLOAD),
            transport=wire.transport,
        )
        person = interrupting if which == "person" else Token(PERSON)
        with pytest.raises(type(error)) as caught:
            await ask_once(answerer, person, message=QUESTION)
        return caught.value

    raised = asyncio.run(drive())

    assert raised is error
    assert (raised.__context__, raised.__cause__) == (None, None)


@dataclass(frozen=True)
class _Leak:
    """One way an ask can go, with the arguments that drive it."""

    reply: Reply
    person: str | BaseException = PERSON
    workload: str | BaseException = WORKLOAD
    message: str = QUESTION


_LEAK_CASES = {
    "success": _Leak(reply_with(*whole(event_frames(*GOOD_ANSWER)))),
    "refusal": _Leak(refusal_reply(RejectionCode.BUSY, 429, **{"retry-after": "10"})),
    "person-raises": _Leak(reply_with(), person=RuntimeError(PERSON)),
    "workload-raises": _Leak(reply_with(), workload=RuntimeError(WORKLOAD)),
    "person-raises-before-awaiting": _Leak(
        reply_with(), person=RaisedBeforeAwaitingError(PERSON)
    ),
    "person-raises-exception-group": _Leak(
        reply_with(),
        person=BaseExceptionGroup("g", [RuntimeError(PERSON), CustomBaseError()]),
    ),
    "workload-raises-custom-base-exception": _Leak(
        reply_with(), workload=CustomBaseError(WORKLOAD)
    ),
    "person-crlf": _Leak(reply_with(), person=f"{PERSON}\r\n"),
    "workload-space": _Leak(reply_with(), workload=f"{WORKLOAD} x"),
    "workload-non-ascii": _Leak(reply_with(), workload=f"{WORKLOAD}é"),
    "surrogate": _Leak(reply_with(), message=f"{QUESTION}\ud800"),
    "refusal-echoes-both-tokens": _Leak(
        Reply(
            status=403,
            headers={"content-type": "application/json"},
            chunks=[
                RunRejection(
                    v=2, code=RejectionCode.FORBIDDEN, message=f"{PERSON} {WORKLOAD}"
                )
                .model_dump_json()
                .encode()
            ],
        )
    ),
    "failed-message": _Leak(
        reply_with(*event_frames(run_started(), failed(FailureCode.PROVIDER_ERROR)))
    ),
    "violation-detail": _Leak(reply_with(violating_frame())),
    "connection-dropped": _Leak(
        Reply(
            chunks=whole(event_frames(run_started(), assistant_text("Hallo"))),
            breaks_after_chunks=True,
        )
    ),
    "cannot-connect": _Leak(Reply(refuses_connection=True)),
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

    async def drive() -> Outcome:
        if transport == "mock":
            return await an_asker(reply, person=person, workload=workload).ask(
                message=message
            )
        async with fake_service(lambda _question: reply) as url:
            runs_url = closed_port_origin() if reply.refuses_connection else url
            answerer = ServiceAnswerer(origin=runs_url, workload_token=Token(workload))
            return await ask_once(answerer, Token(person), message=message)

    outcome = asyncio.run(drive())

    errors = [] if outcome.error is None else [unavailable(outcome)]
    shown = everything_shown(caplog.records, errors)
    assert not [marker for marker in MARKERS if marker in shown]


_WALK_MARKER = "walk-MARKER"


def _a_record(*, args: object = (), exc_info: object = None) -> logging.LogRecord:
    return logging.LogRecord(
        "name",
        logging.INFO,
        "path",
        1,
        "%s" if args else "plain",
        args,  # pyright: ignore[reportArgumentType]
        exc_info,  # pyright: ignore[reportArgumentType]
    )


def _chained_error() -> ValueError:
    error = ValueError("outer")
    error.__context__ = RuntimeError(_WALK_MARKER)
    return error


def _cyclic_error() -> RuntimeError:
    error = RuntimeError("outer")
    error.__context__ = RuntimeError(_WALK_MARKER)
    error.__context__.__context__ = error
    return error


@pytest.mark.parametrize(
    "shown",
    [
        pytest.param(
            everything_shown([_a_record(args=({"k": [_WALK_MARKER]},))]), id="args"
        ),
        pytest.param(everything_shown(errors=[_chained_error()]), id="context"),
        pytest.param(everything_shown(errors=[_cyclic_error()]), id="cycle"),
        pytest.param(
            everything_shown(
                errors=[BaseExceptionGroup("group", [RuntimeError(_WALK_MARKER)])]
            ),
            id="group-member",
        ),
        pytest.param(
            everything_shown(
                [_a_record(exc_info=(RuntimeError, _chained_error(), None))]
            ),
            id="exc-info",
        ),
    ],
)
def test_a_marker_held_anywhere_is_found(shown: str) -> None:
    assert _WALK_MARKER in shown


def test_a_record_and_an_error_without_the_marker_show_none() -> None:
    assert _WALK_MARKER not in everything_shown([_a_record()], [RuntimeError("other")])
