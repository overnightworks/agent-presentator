"""The process starts without an API key and talks to the speech service port."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from copresenter.answer import provider_runtime
from copresenter.config import Settings, Transport
from copresenter.main import private_listener

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


def unix_settings(directory: Path) -> Settings:
    return Settings(
        transport=Transport.UNIX,
        socket_directory=directory,
        runtime_uid=os.geteuid(),
    )


def test_unix_start_prebinds_an_owned_private_socket(tmp_path: Path) -> None:
    directory = tmp_path / "private"

    with private_listener(unix_settings(directory)) as listener:
        socket_status = (directory / "copresenter.sock").lstat()
        assert listener.family == socket.AF_UNIX
        assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
        assert stat.S_IMODE(socket_status.st_mode) == 0o600
        assert socket_status.st_uid == os.geteuid()

    assert not (directory / "copresenter.sock").exists()


def test_unix_start_refuses_and_preserves_a_preexisting_path(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "copresenter.sock"
    path.write_text("belongs to another process", encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="already exists"),
        private_listener(unix_settings(directory)),
    ):
        pass

    assert path.read_text(encoding="utf-8") == "belongs to another process"


def test_unix_transport_requires_an_absolute_directory() -> None:
    with pytest.raises(ValueError, match="absolute"):
        Settings(
            transport=Transport.UNIX,
            socket_directory=Path("relative/private"),
            runtime_uid=os.geteuid(),
        )


@pytest.mark.parametrize("bad_directory", ["symlink", "open-mode", "file"])
def test_unix_start_refuses_an_unowned_directory_shape(
    tmp_path: Path,
    bad_directory: str,
) -> None:
    directory = tmp_path / "private"
    if bad_directory == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
    elif bad_directory == "open-mode":
        directory.mkdir(mode=0o755)
    else:
        directory.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError), private_listener(unix_settings(directory)):
        pass


def test_cleanup_does_not_unlink_a_replacement_inode(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    path = directory / "copresenter.sock"
    replacement: socket.socket | None = None

    with private_listener(unix_settings(directory)):
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(path))

    assert path.exists()
    assert replacement is not None
    replacement.close()
    path.unlink()


def test_unix_start_requires_the_process_uid(tmp_path: Path) -> None:
    settings = Settings(
        transport=Transport.UNIX,
        socket_directory=tmp_path / "private",
        runtime_uid=os.geteuid() + 1,
    )

    with pytest.raises(RuntimeError, match="euid"), private_listener(settings):
        pass
