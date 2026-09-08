"""The process starts without an API key and talks to the speech service port."""

from __future__ import annotations

import pytest

from copresenter.answer import provider_runtime
from copresenter.config import Settings

from .conftest import ALLOWED_ORIGIN


def test_provider_runtime_carries_the_required_codex_bounds() -> None:
    config = provider_runtime("claude-sonnet-4-6")

    assert config.codex_max_concurrent_processes == 1
    assert config.codex_max_concurrent_image_runs == 1
    assert config.anthropic_api_key is None
    assert config.secret_env_keys == ("ANTHROPIC_API_KEY",)
    assert config.mcp_server is None
    assert config.claude_cli_binary == "claude"
    assert config.claude_chat_model == "claude-sonnet-4-6"


def test_settings_default_speech_url_is_the_speech_service_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPRESENTER_ALLOWED_ORIGIN", ALLOWED_ORIGIN)
    monkeypatch.delenv("COPRESENTER_SPEECH_URL", raising=False)

    assert Settings().speech_url == "http://127.0.0.1:8090"


def test_settings_standin_port_is_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPRESENTER_ALLOWED_ORIGIN", ALLOWED_ORIGIN)
    monkeypatch.setenv("COPRESENTER_SPEECH_URL", "http://127.0.0.1:8765")

    assert Settings().speech_url == "http://127.0.0.1:8765"
