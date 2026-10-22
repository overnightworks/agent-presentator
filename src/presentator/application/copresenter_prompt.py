"""The turn the answering model is given: the question and the deck around it."""

from presentator.contracts.copresenter import DeckText, Question, SlideText


def user_message(question: Question, deck_text: DeckText) -> str:
    """What was said, the current slide, the deck around it."""
    pages = "\n\n".join(_page(slide) for slide in deck_text.slides)
    current = deck_text.at(question.slide)
    here = (
        f"Folie {question.slide} is not in this deck."
        if current is None
        else _page(current, current_mark=True)
    )
    return (
        f"Deck: {deck_text.title}\n"
        f"You are on slide {question.slide}.\n\n"
        f"--- CURRENT SLIDE ---\n{here}\n--- END CURRENT SLIDE ---\n\n"
        f"--- DECK ---\n{pages}\n--- END DECK ---\n\n"
        f"The person said: {question.said}"
    )


def _page(slide: SlideText, *, current_mark: bool = False) -> str:
    mark = " (current)" if current_mark else ""
    notes = f"\nSpeaker notes: {slide.notes}" if slide.notes else ""
    return f"Slide {slide.number}{mark}: {slide.title}\n{slide.body}{notes}"
