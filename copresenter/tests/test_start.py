"""The process starts without an API key and talks to the speech service port."""

from __future__ import annotations

import fcntl
import os
import socket
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from agent_providers.config import (
    ClaudeCliRuntime,
    TurnRuntimeConfig,
    current_config,
    reset_config,
)

from copresenter import main as copresenter_main
from copresenter.answer import provider_runtime
from copresenter.config import Settings, Transport
from copresenter.main import private_listener

from .conftest import ALLOWED_ORIGIN, EXAMPLE_DECK


def test_provider_runtime_sets_only_what_a_claude_turn_reads(tmp_path: Path) -> None:
    token_file = tmp_path / "claude.token"
    search_path = (tmp_path / "bin",)
    working_root = tmp_path / "root"

    config = provider_runtime(
        token_file=token_file,
        search_path=search_path,
        working_root=working_root,
    )

    assert config.claude == ClaudeCliRuntime(binary="claude", token_file=token_file)
    assert config.cli_binary_search_path == search_path
    assert config.cli_working_directory_root == working_root
    assert config.turns == TurnRuntimeConfig(mcp_server=None)
    assert (config.grok, config.codex) == (None, None)
    assert (config.anthropic_api_key, config.xai_api_key, config.openai_api_key) == (
        None,
        None,
        None,
    )


def test_settings_default_to_the_shared_login_and_a_private_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "COPRESENTER_CLAUDE_TOKEN_FILE",
        "COPRESENTER_CLI_SEARCH_PATH",
        "COPRESENTER_CLI_WORKING_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(allowed_origin=ALLOWED_ORIGIN)

    assert settings.claude_token_file == tmp_path / ".secrets/agent-service/claude/token"
    assert settings.cli_search_path == (tmp_path / ".local/bin",)
    assert settings.cli_working_root == (
        tmp_path / ".local/state/agent-presentator/copresenter-cli"
    )


def test_settings_read_the_search_path_like_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPRESENTER_ALLOWED_ORIGIN", ALLOWED_ORIGIN)
    monkeypatch.setenv("COPRESENTER_CLI_SEARCH_PATH", "/opt/a:/opt/b")

    assert Settings().cli_search_path == (Path("/opt/a"), Path("/opt/b"))


@pytest.fixture
def unconfigured_provider_runtime() -> Iterator[None]:
    reset_config()
    yield
    reset_config()


@pytest.mark.usefixtures("unconfigured_provider_runtime")
def test_start_makes_the_working_root_and_its_missing_parents_private_in_an_empty_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    (home / ".local").mkdir()
    (home / ".local").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COPRESENTER_ALLOWED_ORIGIN", ALLOWED_ORIGIN)
    monkeypatch.setenv("COPRESENTER_DECK", str(EXAMPLE_DECK))
    monkeypatch.delenv("COPRESENTER_CLI_WORKING_ROOT", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    copresenter_main.main()

    root = home / ".local/state/agent-presentator/copresenter-cli"
    for created in (home / ".local/state", root.parent, root):
        assert stat.S_IMODE(created.lstat().st_mode) == 0o700
    for existing in (home, home / ".local"):
        assert stat.S_IMODE(existing.lstat().st_mode) == 0o755
    assert current_config().cli_working_directory_root == root


@pytest.mark.usefixtures("unconfigured_provider_runtime")
def test_start_refuses_a_working_root_with_another_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o755)
    monkeypatch.setenv("COPRESENTER_ALLOWED_ORIGIN", ALLOWED_ORIGIN)
    monkeypatch.setenv("COPRESENTER_DECK", str(EXAMPLE_DECK))
    monkeypatch.setenv("COPRESENTER_CLI_WORKING_ROOT", str(root))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="mode 0700"):
        copresenter_main.main()


@pytest.mark.parametrize(
    "variable",
    [
        "COPRESENTER_CLAUDE_TOKEN_FILE",
        "COPRESENTER_CLI_SEARCH_PATH",
        "COPRESENTER_CLI_WORKING_ROOT",
    ],
)
def test_settings_refuse_a_relative_path_for_the_login_and_the_cli_directories(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv("COPRESENTER_ALLOWED_ORIGIN", ALLOWED_ORIGIN)
    monkeypatch.setenv(variable, "~/.local/state/x")

    with pytest.raises(ValueError, match="not an absolute path"):
        Settings()


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
        directory.chmod(0o755)
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


def test_unix_start_replaces_a_dead_leftover_socket(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "copresenter.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()

    with private_listener(unix_settings(directory)) as listener:
        assert listener.family == socket.AF_UNIX
        socket_status = path.lstat()
        assert stat.S_ISSOCK(socket_status.st_mode)

    assert not path.exists()


def test_unix_start_refuses_a_socket_with_a_live_listener(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "copresenter.sock"
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listening.bind(str(path))
    listening.listen()

    try:
        with (
            pytest.raises(RuntimeError, match="already exists"),
            private_listener(unix_settings(directory)),
        ):
            pass

        assert stat.S_ISSOCK(path.lstat().st_mode)
    finally:
        listening.close()
        path.unlink()


def test_concurrent_start_refuses_while_the_listener_is_running(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"

    with (
        private_listener(unix_settings(directory)),
        pytest.raises(RuntimeError, match="starting or running"),
        private_listener(unix_settings(directory)),
    ):
        pass


def test_reclaim_refuses_when_the_socket_is_replaced_mid_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "copresenter.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()

    def swap_then_report_dead(swapped_path: Path) -> bool:
        swapped_path.unlink()
        swapped_path.write_text("replacement", encoding="utf-8")
        return True

    monkeypatch.setattr(copresenter_main, "_probe_is_dead", swap_then_report_dead)

    with (
        pytest.raises(RuntimeError, match="changed during reclamation"),
        private_listener(unix_settings(directory)),
    ):
        pass

    assert path.read_text(encoding="utf-8") == "replacement"


def test_lock_file_survives_release_and_the_next_start_reacquires_its_inode(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    path = directory / "copresenter.sock"
    lock_path = path.with_name(path.name + ".lock")

    with private_listener(unix_settings(directory)):
        pass

    first_inode = lock_path.stat().st_ino

    with private_listener(unix_settings(directory)):
        assert lock_path.stat().st_ino == first_inode


def test_concurrent_start_refuses_when_another_process_holds_the_released_lock_inode(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    path = directory / "copresenter.sock"
    lock_path = path.with_name(path.name + ".lock")

    with private_listener(unix_settings(directory)):
        pass  # starter A acquired, bound, and released; the lock path's inode remains

    # starter B takes that same, still-existing lock inode directly, as it would
    # between A's release and C's next attempt.
    holder_b = os.open(lock_path, os.O_RDWR)
    fcntl.flock(holder_b, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with (
            pytest.raises(RuntimeError, match="starting or running"),
            private_listener(unix_settings(directory)),
        ):
            pass
    finally:
        fcntl.flock(holder_b, fcntl.LOCK_UN)
        os.close(holder_b)


def test_unix_start_requires_the_process_uid(tmp_path: Path) -> None:
    settings = Settings(
        transport=Transport.UNIX,
        socket_directory=tmp_path / "private",
        runtime_uid=os.geteuid() + 1,
    )

    with pytest.raises(RuntimeError, match="euid"), private_listener(settings):
        pass
