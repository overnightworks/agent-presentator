"""The composition root builds a deck through the build worker when told to."""

import os
import socket
from pathlib import Path

import pytest

from presentator.adapters.build_wire import SOCKET_NAME
from presentator.adapters.build_worker import WorkerToolchain
from presentator.host import main
from presentator.host.config import ConfigurationError, load_settings
from presentator.host.worker_serving import emergency_end

BUILD_TIMEOUT_SECONDS = 123.0
OUTPUT_MEGABYTES = 77


def a_worker_run(environment: pytest.MonkeyPatch, socket_path: Path) -> None:
    environment.setenv("PRESENTATOR_BUILD_RUNNER", "worker")
    environment.setenv("PRESENTATOR_BUILD_WORKER_SOCKET", str(socket_path))
    environment.setenv("PRESENTATOR_BUILD_TIMEOUT_SECONDS", str(BUILD_TIMEOUT_SECONDS))
    environment.setenv("PRESENTATOR_BUILD_OUTPUT_MEGABYTES", str(OUTPUT_MEGABYTES))


def test_an_instance_whose_worker_socket_is_not_the_workers_refuses_to_start_at_once(
    environment: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cell B14: the branch of the root, reached without the connect window."""
    volume = tmp_path / "volume"
    volume.mkdir(mode=0o700)
    placeholder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    placeholder.bind(str(volume / SOCKET_NAME))
    a_worker_run(environment, volume / SOCKET_NAME)

    with placeholder, pytest.raises(ConfigurationError, match="build worker"):
        main.build_instance(load_settings())


def test_an_instance_starts_on_a_worker_that_answers_with_the_workers_own_bounds(
    environment: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review need Q4: the seam fields reach the client as one production value."""
    built: list[WorkerToolchain] = []

    def answering(toolchain: WorkerToolchain) -> bool:
        built.append(toolchain)
        return True

    a_worker_run(environment, tmp_path / "worker.sock")
    environment.setattr(WorkerToolchain, "is_answering", answering)

    main.build_instance(load_settings())

    (toolchain,) = built
    assert toolchain.socket_path == tmp_path / "worker.sock"
    assert toolchain.megabytes == OUTPUT_MEGABYTES
    assert toolchain.reply_bound_seconds == emergency_end(0.0, BUILD_TIMEOUT_SECONDS)
    assert (toolchain.expected_uid, toolchain.expected_gid) == (0, os.getegid())
