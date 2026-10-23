"""The agent service's origin: required, and one canonical http or https origin."""

import pytest

from presentator.host.config import ConfigurationError, load_settings

_SETTING = "PRESENTATOR_AGENT_SERVICE"


def test_an_instance_without_the_agent_service_refuses_to_start(
    environment: pytest.MonkeyPatch,
) -> None:
    """Cell 25."""
    environment.delenv(_SETTING)

    with pytest.raises(ConfigurationError, match="agent_service"):
        load_settings()


@pytest.mark.parametrize(
    "origin",
    ["http://agent-service:8080", "https://agents.example.test"],
)
def test_the_agent_service_takes_a_canonical_http_or_https_origin(
    environment: pytest.MonkeyPatch, origin: str
) -> None:
    """Cell 25."""
    environment.setenv(_SETTING, origin)

    assert load_settings().agent_service == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://agent-service:8080/",
        "http://agent-service:8080/runs",
        "http://user:secret-MARKER@agent-service:8080",
        "http://agent-service:8080?token=secret-MARKER",
        "http://agent-service:8080#secret-MARKER",
        "ftp://agent-service:8080",
        "http://agent-service:secret-MARKER",
        "agent-service:8080",
    ],
)
def test_the_agent_service_refuses_anything_but_an_origin_without_quoting_it(
    environment: pytest.MonkeyPatch, origin: str
) -> None:
    """Cell 25."""
    environment.setenv(_SETTING, origin)

    with pytest.raises(ConfigurationError, match="agent_service") as refused:
        load_settings()

    assert "MARKER" not in str(refused.value)
    assert origin not in str(refused.value)
