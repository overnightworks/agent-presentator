"""GET /health answers the contract, including while models are still loading."""

from fastapi.testclient import TestClient

from tests.conftest import SAMPLE_RATE, FakeHearing, FakeSpeaking, an_app


def test_health_answers_while_models_are_not_ready() -> None:
    client = TestClient(
        an_app(FakeSpeaking(ready=False), FakeHearing(ready=False)),
    )

    body = client.get("/health").json()

    assert body["speaking"] == {"model": "fake-voice", "ready": False}
    assert body["hearing"] == {"model": "fake-ears", "ready": False}
    assert body["sample_rate"] == SAMPLE_RATE
    assert isinstance(body["card_memory_mb"], int)


def test_health_reports_ready_models() -> None:
    body = TestClient(an_app()).get("/health").json()

    assert body["speaking"]["ready"] is True
    assert body["hearing"]["ready"] is True
