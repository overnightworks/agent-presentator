"""The application owns each private co-presenter operation it starts."""

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from presentator.application.copresenter import (
    AskLimiter,
    AskTicket,
    CoPresenterUse,
    PreparedAsk,
    RetryAfter,
)
from presentator.contracts.copresenter import (
    AccessRefusal,
    AnswerText,
    CoPresenterReadiness,
    CoPresenterUnavailableError,
    Done,
    PersonToken,
    Question,
)
from tests.api.copresenter_support import ASKED, PrivateFakes, a_use
from tests.application.fakes import FakeDeckStore, FrozenClock

A_QUESTION = Question(said="Frage", slide=1, language=None)
FIRST_AUDIO = "presentator.application.copresenter"


def a_prepared_ask(use: CoPresenterUse) -> PreparedAsk:
    prepared = asyncio.run(use.prepare(ASKED, "session"))
    assert isinstance(prepared, PreparedAsk)
    return prepared


def test_preparing_an_ask_holds_the_sessions_fresh_person_token() -> None:
    fakes = PrivateFakes()
    use = a_use(fakes, FakeDeckStore())

    prepared = a_prepared_ask(use)

    assert prepared.person == PersonToken("person-token")
    assert prepared.asked == ASKED
    assert fakes.person_access.sessions == ["session"]
    assert fakes.answers.messages == []


def test_a_refused_person_token_is_returned_before_any_answer() -> None:
    fakes = PrivateFakes()
    fakes.person_access.refusal = AccessRefusal.SIGN_IN_AGAIN
    use = a_use(fakes, FakeDeckStore())

    refused = asyncio.run(use.prepare(ASKED, "session"))

    assert refused is AccessRefusal.SIGN_IN_AGAIN
    assert fakes.answers.messages == []


def test_a_failing_token_source_is_unavailable() -> None:
    fakes = PrivateFakes()
    fakes.person_access.unavailable = True
    use = a_use(fakes, FakeDeckStore())

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(use.prepare(ASKED, "session"))


async def _read_until_done(use: CoPresenterUse, prepared: PreparedAsk) -> list[object]:
    async with use.answer(prepared, A_QUESTION) as events:
        return [event async for event in events]


def test_the_service_gets_the_asked_decks_words_and_the_persons_token() -> None:
    fakes = PrivateFakes()
    use = a_use(fakes, FakeDeckStore())

    events = asyncio.run(_read_until_done(use, a_prepared_ask(use)))

    assert events[-1] == Done(text="Eine Antwort.")
    assert fakes.texts.asked == [(ASKED.deck, ASKED.source)]
    assert fakes.answers.person_tokens == ["person-token"]
    (message,) = fakes.answers.messages
    assert message.startswith("Deck: Kundenfeedback\nYou are on slide 1.")
    assert "The person said: Frage" in message


@pytest.mark.parametrize("unreadable", [False, True], ids=["no-slides", "no-mirror"])
def test_a_deck_without_words_never_reaches_the_service(*, unreadable: bool) -> None:
    fakes = PrivateFakes()
    fakes.texts.text = None
    fakes.texts.unreadable = unreadable
    use = a_use(fakes, FakeDeckStore())
    prepared = a_prepared_ask(use)

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(_read_until_done(use, prepared))

    assert fakes.answers.messages == []


def test_cancelling_an_answer_awaits_the_private_operation() -> None:
    asyncio.run(_cancel_answer())


async def _cancel_answer() -> None:
    fakes = PrivateFakes()
    fakes.answers.hold = True
    use = a_use(fakes, FakeDeckStore())
    prepared = await use.prepare(ASKED, "session")
    assert isinstance(prepared, PreparedAsk)

    async with use.answer(prepared, A_QUESTION) as events:
        assert await anext(events) == AnswerText(text="Eine Antwort.")

    assert fakes.answers.exits == 1


def test_ending_hearing_closes_and_awaits_the_private_child() -> None:
    asyncio.run(_end_hearing())


async def _end_hearing() -> None:
    fakes = PrivateFakes()
    use = a_use(fakes, FakeDeckStore())

    async with use.hear("de") as hearing:
        await hearing.send_pcm(b"pcm")

    assert fakes.speech.hearing.frames == [b"pcm"]
    assert fakes.speech.hearing.closed == 1


def test_readiness_comes_from_speech_and_never_touches_the_answer_port() -> None:
    fakes = PrivateFakes()

    readiness = asyncio.run(a_use(fakes, FakeDeckStore()).readiness())

    assert readiness == CoPresenterReadiness(
        hearing_sample_rate=16_000, local_hearing_ready=True
    )
    assert fakes.speech.readiness_calls == 1
    assert (fakes.answers.messages, fakes.answers.exits) == ([], 0)


def test_the_first_audio_is_logged_once_with_its_delay_and_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fakes = PrivateFakes()
    fakes.answers.deltas = ["Erster Satz. ", "Zweiter Satz."]
    ticks = iter([10.0, 10.25, 99.0])
    use = replace(a_use(fakes, FakeDeckStore()), monotonic=lambda: next(ticks))
    caplog.set_level(logging.INFO, logger=FIRST_AUDIO)

    asyncio.run(_read_until_done(use, a_prepared_ask(use)))

    assert [record.getMessage() for record in caplog.records] == [
        "co-presenter first audio: ms=250"
    ]


def test_an_answer_without_audio_logs_no_first_audio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fakes = PrivateFakes()
    fakes.answers.deltas = []
    use = a_use(fakes, FakeDeckStore())
    caplog.set_level(logging.INFO, logger=FIRST_AUDIO)

    asyncio.run(_read_until_done(use, a_prepared_ask(use)))

    assert caplog.records == []


def a_limiter() -> AskLimiter:
    return AskLimiter(clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)))


def test_a_released_old_ticket_cannot_free_the_slot_of_a_newer_answer() -> None:
    limiter = a_limiter()
    old = limiter.admit("session")
    assert isinstance(old, AskTicket)
    old.release()
    newer = limiter.admit("session")
    assert isinstance(newer, AskTicket)

    old.release()

    assert limiter.admit("session") == RetryAfter(1)


def test_sessions_that_asked_once_and_went_quiet_leave_no_record() -> None:
    clock = FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC))
    limiter = AskLimiter(clock=clock)
    for name in ("quiet-one", "quiet-two"):
        ticket = limiter.admit(name)
        assert isinstance(ticket, AskTicket)
        ticket.release()
    clock.advance(limiter.limits.window + timedelta(seconds=1))

    assert isinstance(limiter.admit("late"), AskTicket)

    assert "quiet-one" not in repr(limiter)
    assert "quiet-two" not in repr(limiter)


THREADS = 8
DEADLOCK_SECONDS = 10


class ObservedLock:
    """A real lock that reports entry attempts and can hold its next holder inside."""

    def __init__(self) -> None:
        """Start with nobody pausing inside."""
        self._lock = threading.Lock()
        self._bookkeeping = threading.Lock()
        self._attempts = 0
        self._pausing = False
        self.holder_inside = threading.Event()
        self.attempted_by_all = threading.Event()
        self._holder_may_leave = threading.Event()

    def hold_next_holder_until_all_attempted(self) -> None:
        self._attempts = 0
        self._pausing = True

    def let_the_holder_leave(self) -> None:
        self._holder_may_leave.set()

    def __enter__(self) -> bool:
        """Announce the attempt, then take the lock; pause inside when asked to."""
        with self._bookkeeping:
            self._attempts += 1
            if self._attempts == THREADS:
                self.attempted_by_all.set()
        self._lock.acquire()
        if self._pausing:
            self._pausing = False
            self.holder_inside.set()
            if not self._holder_may_leave.wait(timeout=DEADLOCK_SECONDS):
                self._lock.release()
                raise TimeoutError
        return True

    def __exit__(self, *_exception: object) -> None:
        """Give the lock back."""
        self._lock.release()


def run_on_many_threads_while_one_holds_the_lock[Result](
    guard: ObservedLock, action: Callable[[], Result]
) -> list[Result]:
    guard.hold_next_holder_until_all_attempted()
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(action) for _ in range(THREADS)]
        try:
            assert guard.holder_inside.wait(timeout=DEADLOCK_SECONDS)
            assert guard.attempted_by_all.wait(timeout=DEADLOCK_SECONDS)
        finally:
            guard.let_the_holder_leave()
        return [future.result() for future in futures]


def test_simultaneous_asks_of_one_session_on_many_threads_admit_exactly_one() -> None:
    guard = ObservedLock()
    limiter = AskLimiter(
        clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)), guard=guard
    )

    outcomes = run_on_many_threads_while_one_holds_the_lock(
        guard, lambda: limiter.admit("session")
    )

    admitted = [outcome for outcome in outcomes if isinstance(outcome, AskTicket)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, RetryAfter)]
    assert len(admitted) == 1
    assert refused == [RetryAfter(1)] * (THREADS - 1)


def test_simultaneous_releases_of_one_ticket_free_the_slot_exactly_once() -> None:
    guard = ObservedLock()
    limiter = AskLimiter(
        clock=FrozenClock(instant=datetime(2026, 1, 1, tzinfo=UTC)), guard=guard
    )
    old = limiter.admit("session")
    assert isinstance(old, AskTicket)

    run_on_many_threads_while_one_holds_the_lock(guard, old.release)

    replacement = limiter.admit("session")
    assert isinstance(replacement, AskTicket)
    old.release()
    assert limiter.admit("session") == RetryAfter(1)
