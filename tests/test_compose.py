"""What the compose file promises the server about this machine's Docker.

The server is handed names it later gives the daemon, and a name that drifts
from what Docker really made is a build reading somebody else's talks or none
at all. Nothing here starts a container; it reads the one file that must agree
with itself.
"""

from pathlib import Path

import pytest

_COMPOSE = Path(__file__).parents[1] / "compose.yaml"
_THE_BUILDS_VOLUME = "builds"


def test_the_server_is_handed_the_very_volume_this_file_declares() -> None:
    written = _COMPOSE.read_text(encoding="utf-8")

    # Docker names a declared volume after the project it belongs to, so the
    # name the server is given and the volume that exists agree exactly while
    # this file derives the one and pins neither.
    assert (
        f"PRESENTATOR_BUILD_VOLUME: ${{COMPOSE_PROJECT_NAME}}_{_THE_BUILDS_VOLUME}"
        in written
    )
    lines = written.splitlines()
    assert f"  {_THE_BUILDS_VOLUME}:" in lines
    assert not [line for line in lines if line.strip().startswith("name:")]


def test_copresenter_uses_one_private_socket_and_runtime_uid() -> None:
    written = _COMPOSE.read_text(encoding="utf-8")

    services_using_uid = ("presentator", "build-sandbox")
    configured_uid = "PRESENTATOR_RUNTIME_UID: ${PRESENTATOR_RUNTIME_UID:"
    assert written.count(configured_uid) == len(services_using_uid)
    assert (
        "PRESENTATOR_COPRESENTER_SOCKET: /run/presentator-copresenter/copresenter.sock"
        in written
    )
    assert "PRESENTATOR_SPEECH_SOCKET: /run/presentator-speech/speech.sock" in written
    assert ":/run/presentator-copresenter" in written
    assert ":/run/presentator-speech" in written
    assert "extra_hosts:" not in written
    assert "3040:3040" not in written
    assert "8090:8090" not in written
    assert written.count("ports:") == 1
    assert written.count('"127.0.0.1:8000:8000"') == 1


def _service_lines(name: str) -> list[str]:
    """The lines of one service, comments dropped, from its name to the next."""
    lines = _COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:") + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index] and not lines[index].startswith("   ")
        ),
        len(lines),
    )
    return [line for line in lines[start:end] if not line.strip().startswith("#")]


@pytest.mark.parametrize(
    "sealing_line",
    [
        '    user: "0:presentator"',
        "    cap_drop:\n      - ALL",
        "    cap_add:\n      - SETUID\n      - SETGID\n      - KILL",
        "    security_opt:\n      - no-new-privileges",
        "    read_only: true",
        "    network_mode: none",
        "    ipc: private",
        "    shm_size: 256m",
        "    pids_limit: 512",
        "    restart: always",
        "    stop_signal: SIGKILL",
        "    pull_policy: never",
        "      - /work:size=2g,nr_inodes=200k,mode=755,nosuid,nodev",
        "      - /tmp:size=512m,nr_inodes=10k,mode=1777,nosuid,nodev",
        "        source: build-socket\n        target: /run/presentator-build",
        "        volume:\n          nocopy: true",
        "      - PRESENTATOR_BUILD_TIMEOUT_SECONDS",
        "      - PRESENTATOR_BUILD_OUTPUT_MEGABYTES",
    ],
)
def test_the_worker_service_carries_each_key_that_seals_it(sealing_line: str) -> None:
    assert sealing_line in "\n".join(_service_lines("build-worker"))


def test_the_worker_swap_equals_its_memory_so_swap_cannot_double_the_bound() -> None:
    keys = {
        key: value
        for key, _, value in (
            line.strip().partition(": ") for line in _service_lines("build-worker")
        )
    }

    assert keys["memswap_limit"] == keys["mem_limit"] == "4g"


@pytest.mark.parametrize("absent_key", ["env_file", "init", "healthcheck", "build"])
def test_the_worker_service_has_no_key_that_would_unseal_or_rebuild_it(
    absent_key: str,
) -> None:
    keys = {line.strip().partition(":")[0] for line in _service_lines("build-worker")}

    assert absent_key not in keys


def test_the_socket_volume_starts_as_a_private_root_directory() -> None:
    written = _COMPOSE.read_text(encoding="utf-8")

    assert "      o: size=16k,nr_inodes=8,mode=0750,uid=0" in written
