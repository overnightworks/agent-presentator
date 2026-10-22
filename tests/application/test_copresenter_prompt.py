"""The turn the answering model is given."""

from presentator.application.copresenter_prompt import user_message
from presentator.contracts.copresenter import DeckText, Question, SlideText

_DECK = DeckText(
    title="Kunden",
    language=None,
    slides=(
        SlideText(number=1, title="Start", body="# Start", notes=""),
        SlideText(number=2, title="Zahlen", body="# Zahlen", notes="say it slowly"),
    ),
)


def asked_on(slide: int) -> Question:
    return Question(said="Was heisst das?", slide=slide, language=None)


def test_the_message_marks_the_current_slide_and_carries_its_notes() -> None:
    message = user_message(asked_on(2), _DECK)

    assert (
        "Slide 2 (current): Zahlen\n# Zahlen\nSpeaker notes: say it slowly" in message
    )
    assert "Slide 1: Start" in message


def test_the_message_carries_the_title_and_what_was_said() -> None:
    message = user_message(asked_on(1), _DECK)

    assert message.startswith("Deck: Kunden\nYou are on slide 1.")
    assert message.endswith("The person said: Was heisst das?")


def test_a_slide_the_deck_does_not_have_is_said_to_be_missing() -> None:
    message = user_message(asked_on(9), _DECK)

    assert "Folie 9 is not in this deck." in message
