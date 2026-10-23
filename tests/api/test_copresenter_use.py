"""The co-presenter use case prepares and answers an ask over the port fakes."""

import asyncio
import logging
from dataclasses import replace

import pytest

from presentator.application.copresenter import CoPresenterUse, PreparedAsk
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
from tests.application.fakes import FakeDeckStore

A_QUESTION = Question(said="Frage", slide=1, language=None)
FIRST_AUDIO = "presentator.application.copresenter"


def a_prepared_ask(use: CoPresenterUse) -> PreparedAsk:
    prepared = asyncio.run(use.prepare(ASKED, "session"))
    assert isinstance(prepared, PreparedAsk)
    return prepared


def test_preparing_an_ask_holds_the_sessions_fresh_person_token() -> None:
    """Decision 3: prepare fetches the session's token once and calls no service."""
    fakes = PrivateFakes()
    use = a_use(fakes, FakeDeckStore())

    prepared = a_prepared_ask(use)

    assert prepared.person == PersonToken("person-token")
    assert prepared.asked == ASKED
    assert fakes.person_access.sessions == ["session"]
    assert fakes.answers.messages == []


def test_a_refused_person_token_is_returned_before_any_answer() -> None:
    """Decision 3, cells 2 and 3: a refusal is returned before the service."""
    fakes = PrivateFakes()
    fakes.person_access.refusal = AccessRefusal.SIGN_IN_AGAIN
    use = a_use(fakes, FakeDeckStore())

    refused = asyncio.run(use.prepare(ASKED, "session"))

    assert refused is AccessRefusal.SIGN_IN_AGAIN
    assert fakes.answers.messages == []


def test_a_failing_token_source_is_unavailable() -> None:
    """Decision 3, cells 4 and 5: a failing token source is unavailable."""
    fakes = PrivateFakes()
    fakes.person_access.unavailable = True
    use = a_use(fakes, FakeDeckStore())

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(use.prepare(ASKED, "session"))


async def _read_until_done(use: CoPresenterUse, prepared: PreparedAsk) -> list[object]:
    async with use.answer(prepared, A_QUESTION) as events:
        return [event async for event in events]


def test_the_service_gets_the_asked_decks_words_and_the_persons_token() -> None:
    """Decision 3, cell 1: the message holds the asked deck's words."""
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
    """Decision 3, cells 20 and 21."""
    fakes = PrivateFakes()
    fakes.texts.text = None
    fakes.texts.unreadable = unreadable
    use = a_use(fakes, FakeDeckStore())
    prepared = a_prepared_ask(use)

    with pytest.raises(CoPresenterUnavailableError):
        asyncio.run(_read_until_done(use, prepared))

    assert fakes.answers.messages == []


def test_cancelling_an_answer_awaits_the_private_operation() -> None:
    """Unchanged behaviour: cancelling an answer awaits the private operation."""
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
    """Unchanged behaviour: ending a hearing closes its child."""
    asyncio.run(_end_hearing())


async def _end_hearing() -> None:
    fakes = PrivateFakes()
    use = a_use(fakes, FakeDeckStore())

    async with use.hear("de") as hearing:
        await hearing.send_pcm(b"pcm")

    assert fakes.speech.hearing.frames == [b"pcm"]
    assert fakes.speech.hearing.closed == 1


def test_readiness_comes_from_speech_and_never_touches_the_answer_port() -> None:
    """Unchanged behaviour: readiness comes from speech alone."""
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
    """Decision 9: one line, counted from `started`."""
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
    """Decision 9, cell 27."""
    fakes = PrivateFakes()
    fakes.answers.deltas = []
    use = a_use(fakes, FakeDeckStore())
    caplog.set_level(logging.INFO, logger=FIRST_AUDIO)

    asyncio.run(_read_until_done(use, a_prepared_ask(use)))

    assert caplog.records == []
