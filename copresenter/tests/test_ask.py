"""`POST /ask` streams text, then speaks each finished sentence."""

from __future__ import annotations

from copresenter.answer import user_message


def test_user_message_gives_claude_the_current_slide_and_the_deck(example_deck) -> None:
    message = user_message("Was steht hier?", 1, example_deck)

    assert "CURRENT SLIDE" in message
    assert example_deck.at(1).title in message
    assert example_deck.at(1).notes in message
    assert "DECK" in message
    assert example_deck.at(2).title in message
    assert "Was steht hier?" in message


def test_ask_streams_sentences_and_audio(client, fake_speech) -> None:
    with client.stream(
        "POST",
        "/ask",
        json={"said": "Was steht auf dieser Folie?", "slide": 1, "language": "de"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: sentence" in body
    assert "event: audio" in body
    assert "event: done" in body
    assert "wav_b64" in body
    assert fake_speech.spoken
    assert all(language == "de" for _text, language in fake_speech.spoken)
    assert "Folie 1" in body
