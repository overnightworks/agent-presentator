"""`GET /who` names the answering model and the speech models behind it."""

from __future__ import annotations


def test_who_reports_the_canned_answerer_and_speech_health(client) -> None:
    response = client.get("/who")

    assert response.status_code == 200
    body = response.json()
    assert body["answerer"]["provider"] == "canned"
    assert body["answerer"]["model"] == "canned"
    assert body["speech"]["speaking"]["model"] == "stand-in"
    assert body["speech"]["hearing"]["ready"] is True
    assert body["speech"]["reachable"] is True
    assert body["deck"]["title"] == "Co-presenter"
