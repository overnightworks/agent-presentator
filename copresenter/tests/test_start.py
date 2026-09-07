"""The process refuses to start without a provider key in the environment."""

from __future__ import annotations

import pytest

from copresenter.config import MissingProviderKeyError, provider_key


def test_provider_key_refuses_an_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(MissingProviderKeyError, match="ANTHROPIC_API_KEY"):
        provider_key()


def test_provider_key_returns_the_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")

    assert provider_key() == "not-a-real-key"
