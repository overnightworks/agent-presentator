"""The service answerer posts one run to its origin and reads it to its end."""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import TYPE_CHECKING

import pytest
from agent_client.wire import (
    FailureCode,
    RejectionCode,
    RunRequest,
    TurnRole,
)

from presentator.adapters.agent_service import ServiceAnswerer
from presentator.contracts.copresenter import NotEnabledError
from tests.adapters.agent_service_support import (
    GOOD_ANSWER,
    ORIGIN,
    PERSON,
    QUESTION,
    SERVICE_MESSAGE,
    VIOLATION_DETAIL,
    WORKLOAD,
    Outcome,
    Reply,
    Token,
    an_asker,
    assistant_text,
    byte_by_byte,
    cancelled,
    event_frames,
    failed,
    fake_service,
    one_warning,
    refusal_reply,
    reply_with,
    run_started,
    succeeded,
    summary_lines,
    unavailable,
    violating_frame,
    whole,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.adapters.agent_service_support import Step


def test_the_request_is_the_run_request_of_the_contract() -> None:
    asker = an_asker(reply_with(*whole(event_frames(*GOOD_ANSWER))))

    asyncio.run(asker.ask())

    (request,) = asker.wire.requests
    assert request.method == "POST"
    assert str(request.url) == f"{ORIGIN}/runs"
    run = RunRequest.model_validate_json(request.content)
    assert (run.v, run.purpose) == (2, "copresenter")
    assert run.roles == ["admin"]
    assert run.tools == {}
    assert run.model is None
    assert run.turn.system
    assert [(m.role, m.text) for m in run.turn.messages] == [(TurnRole.USER, QUESTION)]
    assert request.extensions["timeout"] == {
        "connect": 5,
        "write": 5,
        "pool": 5,
        "read": 60,
    }


def test_both_tokens_go_with_every_run_and_each_is_awaited_once() -> None:
    asker = an_asker(reply_with(*whole(event_frames(*GOOD_ANSWER))))

    asyncio.run(asker.ask())

    (request,) = asker.wire.requests
    assert request.headers["authorization"] == f"Bearer {PERSON}"
    assert request.headers["agent-workload-token"] == WORKLOAD
    assert request.headers["accept"] == "text/event-stream"
    assert (asker.person.calls, asker.workload.calls) == (1, 1)


@pytest.mark.parametrize("split", [whole, byte_by_byte], ids=["whole", "bytes"])
def test_the_answer_is_the_assistant_text(
    split: Callable[[list[bytes]], list[bytes]],
) -> None:
    asker = an_asker(reply_with(*split(event_frames(*GOOD_ANSWER))))

    outcome = asyncio.run(asker.ask())

    assert outcome == Outcome(["Hallo ", "Welt."], None)


def _extra_in_the_terminals_chunk(frames: list[bytes]) -> list[bytes]:
    return whole(frames)


def _extra_in_a_later_chunk(frames: list[bytes]) -> list[bytes]:
    return [*whole(frames[:3]), frames[3]]


@pytest.mark.parametrize(
    "arrange",
    [_extra_in_the_terminals_chunk, _extra_in_a_later_chunk],
    ids=["same-chunk", "later-chunk"],
)
def test_nothing_may_follow_the_terminal(
    arrange: Callable[[list[bytes]], list[bytes]], caplog: pytest.LogCaptureFixture
) -> None:
    frames = event_frames(
        run_started(), assistant_text("Hallo"), succeeded(), assistant_text("zu viel")
    )
    asker = an_asker(reply_with(*arrange(frames)))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    unavailable(outcome)
    assert "event_after_terminal" in one_warning(caplog)


@pytest.mark.parametrize(
    "code", [code for code in FailureCode if code is not FailureCode.INTERRUPTED]
)
def test_a_failed_run_ends_as_unavailable_and_names_its_code(
    code: FailureCode, caplog: pytest.LogCaptureFixture
) -> None:
    asker = an_asker(
        reply_with(*event_frames(run_started(), assistant_text("Hallo"), failed(code)))
    )

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    unavailable(outcome)
    assert code.value in one_warning(caplog)


@pytest.mark.parametrize(
    ("ending", "named"),
    [
        (failed(FailureCode.INTERRUPTED), "interrupted_without_side_effect"),
        (cancelled(), "cancelled"),
    ],
    ids=["interrupted", "cancelled"],
)
def test_an_interrupted_or_cancelled_run_ends_as_unavailable(
    ending: Step, named: str, caplog: pytest.LogCaptureFixture
) -> None:
    asker = an_asker(
        reply_with(*event_frames(run_started(), assistant_text("Hallo"), ending))
    )

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    unavailable(outcome)
    assert named in one_warning(caplog)


def test_a_stream_without_a_terminal_event_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    asker = an_asker(reply_with(*event_frames(run_started(), assistant_text("Hallo"))))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    unavailable(outcome)
    assert "truncated" in one_warning(caplog)


def test_a_sequence_gap_is_unavailable_and_nothing_after_it_is_yielded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frames = event_frames(
        run_started(),
        assistant_text("Hallo"),
        assistant_text("fehlt"),
        assistant_text("danach"),
    )
    asker = an_asker(reply_with(*whole([frames[0], frames[1], frames[3]])))

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    unavailable(outcome)
    assert "sequence_gap" in one_warning(caplog)


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
    asker = an_asker(refusal_reply(code, _SERVICE_STATUS[code], **headers))

    outcome = asyncio.run(asker.ask())

    unavailable(outcome)
    warning = one_warning(caplog)
    assert code.value in warning
    assert (retry_after or "None") in warning


_UNKNOWN_CODE = b'{"v": 2, "code": "bribe_me", "message": "Dienst sagt nein"}'


@pytest.mark.parametrize(
    "reply",
    [
        Reply(
            status=403,
            headers={"content-type": "application/json"},
            chunks=[_UNKNOWN_CODE],
        ),
        Reply(
            status=502, headers={"content-type": "text/html"}, chunks=[b"<html></html>"]
        ),
        Reply(
            status=200,
            headers={"content-type": "application/json"},
            chunks=[b"{}"],
        ),
        refusal_reply(RejectionCode.BUSY, 200),
        Reply(status=307, headers={"location": "http://elsewhere.test/runs"}),
    ],
    ids=["unknown-code", "html-502", "json-200", "rejection-looking-200", "redirect"],
)
def test_a_refusal_without_a_known_code_is_unavailable_with_code_none(
    reply: Reply, caplog: pytest.LogCaptureFixture
) -> None:
    asker = an_asker(reply)

    outcome = asyncio.run(asker.ask())

    unavailable(outcome)
    warning = one_warning(caplog)
    assert "None" in warning
    assert not [code for code in RejectionCode if code.value in warning]
    assert "bribe_me" not in warning
    assert len(asker.wire.requests) == 1


@pytest.mark.parametrize("retry_after", ["soon", "-5", "1e3", "٣"])
def test_a_retry_after_that_is_not_a_whole_number_of_seconds_is_not_logged(
    retry_after: str, caplog: pytest.LogCaptureFixture
) -> None:
    asker = an_asker(
        refusal_reply(RejectionCode.BUSY, 429, **{"retry-after": retry_after})
    )

    asyncio.run(asker.ask())

    warning = one_warning(caplog)
    assert "retry_after=None" in warning
    assert retry_after not in warning


def test_a_transport_that_cannot_connect_is_unavailable() -> None:
    asker = an_asker(Reply(refuses_connection=True))

    outcome = asyncio.run(asker.ask())

    unavailable(outcome)
    assert asker.wire.transports_built == 1


def test_a_transport_failure_mid_stream_is_unavailable_after_the_text() -> None:
    asker = an_asker(
        Reply(
            chunks=whole(event_frames(run_started(), assistant_text("Hallo"))),
            breaks_after_chunks=True,
        )
    )

    outcome = asyncio.run(asker.ask())

    assert outcome.texts == ["Hallo"]
    unavailable(outcome)


def test_leaving_early_closes_the_response() -> None:
    asker = an_asker(
        Reply(
            chunks=whole(event_frames(run_started(), assistant_text("Hallo"))),
            hangs_after_chunks=True,
        )
    )

    async def leave_after_first_text() -> None:
        async with asker.answerer.answer(
            message=QUESTION, person_token=asker.person
        ) as stream:
            assert await anext(stream) == "Hallo"
        assert asker.wire.response_stream.closed.is_set()

    asyncio.run(leave_after_first_text())


def test_one_asks_end_leaves_another_intact() -> None:
    first_left = asyncio.Event()
    second_frames = event_frames(
        run_started(),
        assistant_text("zweite "),
        assistant_text("Antwort."),
        succeeded(),
    )

    def reply_for(question: str) -> Reply:
        if question == "first":
            return Reply(
                chunks=whole(event_frames(run_started(), assistant_text("erst"))),
                hangs_after_chunks=True,
            )
        return Reply(
            chunks=whole(second_frames[:2]),
            gate=first_left,
            late_chunks=whole(second_frames[2:]),
        )

    async def drive() -> list[str]:
        async with fake_service(reply_for) as url:
            answerer = ServiceAnswerer(origin=url, workload_token=Token(WORKLOAD))
            person = Token(PERSON)
            async with answerer.answer(message="second", person_token=person) as second:
                async with answerer.answer(
                    message="first", person_token=person
                ) as first:
                    assert await anext(first) == "erst"
                first_left.set()
                return [text async for text in second]

    assert asyncio.run(drive()) == ["zweite ", "Antwort."]


@pytest.mark.parametrize(
    "origin", ["http://agent-service.test", "http://agent-service.test/"]
)
def test_the_run_is_posted_to_the_origins_runs_path(origin: str) -> None:
    """#452: the origin with or without a trailing slash posts to /runs."""
    asker = an_asker(reply_with(*whole(event_frames(*GOOD_ANSWER))), origin=origin)

    asyncio.run(asker.ask())

    (request,) = asker.wire.requests
    assert str(request.url) == "http://agent-service.test/runs"


@pytest.mark.parametrize("code", list(RejectionCode))
def test_only_a_not_enabled_refusal_is_a_not_enabled_error(code: RejectionCode) -> None:
    """#452: only not_enabled raises NotEnabledError."""
    asker = an_asker(refusal_reply(code, 403))

    error = unavailable(asyncio.run(asker.ask()))

    assert isinstance(error, NotEnabledError) is (code is RejectionCode.NOT_ENABLED)


def _ticking_clock() -> Callable[[], float]:
    ticks = itertools.count()
    return lambda: float(next(ticks))


def _summary_of_a_run(reply: Reply, caplog: pytest.LogCaptureFixture) -> str:
    caplog.set_level(logging.INFO)
    asker = an_asker(reply, monotonic=_ticking_clock())
    asyncio.run(asker.ask())
    (summary,) = summary_lines(caplog)
    return summary


def test_a_good_run_logs_its_event_types_and_times_counted_from_the_post(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#452: the run summary of a good run."""
    summary = _summary_of_a_run(reply_with(*whole(event_frames(*GOOD_ANSWER))), caplog)

    assert summary == (
        "agent service run summary: events=RunStarted:1,AssistantText:2,"
        "Heartbeat:1,Succeeded:1 violation=None first_text_ms=1000 end_ms=2000"
    )


def test_a_failed_run_logs_one_summary_without_the_service_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#452: the run summary of a failed run."""
    reply = reply_with(
        *event_frames(
            run_started(), assistant_text("Hallo"), failed(FailureCode.PROVIDER_ERROR)
        )
    )

    summary = _summary_of_a_run(reply, caplog)

    assert "events=RunStarted:1,AssistantText:1,Failed:1" in summary
    assert SERVICE_MESSAGE not in summary


def test_a_stream_violation_logs_its_kind_and_not_its_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#452: the run summary of a violation."""
    summary = _summary_of_a_run(reply_with(violating_frame()), caplog)

    assert "events=StreamViolation:1 violation=unsupported_version" in summary
    assert "first_text_ms=None" in summary
    assert VIOLATION_DETAIL not in summary


def test_leaving_before_the_first_read_logs_one_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#452: the run summary of an early leave."""
    caplog.set_level(logging.INFO)
    asker = an_asker(
        reply_with(*whole(event_frames(*GOOD_ANSWER))), monotonic=_ticking_clock()
    )

    async def leave() -> None:
        async with asker.answerer.answer(message=QUESTION, person_token=asker.person):
            pass

    asyncio.run(leave())

    (summary,) = summary_lines(caplog)
    assert summary.endswith("events= violation=None first_text_ms=None end_ms=1000")


def test_a_refusal_before_the_stream_logs_no_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#452: a refusal opens no run, so no summary."""
    caplog.set_level(logging.INFO)
    asker = an_asker(refusal_reply(RejectionCode.BUSY, 429))

    asyncio.run(asker.ask())

    assert summary_lines(caplog) == []
