"""A gated child's exit descriptor and the readiness check over descriptors."""

import errno
import os
import signal
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace

import pytest

from presentator.adapters import worker_children
from presentator.adapters.worker_children import GatedChild, readable_within
from presentator.host.worker_serving import HEADER_SECONDS, LifeEnd, hand_over
from tests.adapters.test_worker_children import OWN_IDENTITY, PATH
from tests.adapters.worker_children_support import (
    accept_time,
    open_descriptor_count,
    serving_with,
)
from tests.test_worker_record_chain import (
    EXITING_SEVEN,
    SLEEPER,
    WAIT_SECONDS,
    ChainSpawn,
    await_absence,
)

SHORT_SECONDS = 0.05


def true_child() -> GatedChild:
    return worker_children.spawn_gated(
        ["/bin/true"], identity=OWN_IDENTITY, environment={"PATH": PATH}, pass_fds=()
    )


def end_child(child: GatedChild) -> None:
    """Kill the child, reap it within the bound and close it, whatever fails."""
    try:
        child.kill()
        assert child.wait_within(WAIT_SECONDS) is not None, "the child was not reaped"
    finally:
        child.close()


def reaped_by_process_id(process_ids: list[int]) -> None:
    """Take down children the production clean-up left behind."""
    for process_id in process_ids:
        with suppress(ProcessLookupError):
            os.kill(process_id, signal.SIGKILL)
        with suppress(ChildProcessError):
            os.waitpid(process_id, 0)


def is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as error:
        return error.errno == errno.EBADF
    return False


def test_a_childs_exit_descriptor_is_readable_only_once_it_exited() -> None:
    child = true_child()
    try:
        before_the_release = readable_within([child.exit_descriptor], SHORT_SECONDS)
        child.release()
        after_the_exit = readable_within([child.exit_descriptor], WAIT_SECONDS)
        status = child.wait_within(WAIT_SECONDS)
    finally:
        end_child(child)

    assert before_the_release == frozenset()
    assert after_the_exit == {child.exit_descriptor}
    assert status == 0


def refused_pidfd(taken: list[int]) -> Callable[[int], int]:
    def refuse(process_id: int) -> int:
        taken.append(process_id)
        raise OSError(errno.EMFILE, "too many open files")

    return refuse


def test_a_refused_pidfd_kills_and_reaps_the_child_and_closes_both_gate_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_ids: list[int] = []
    monkeypatch.setattr(worker_children, "open_pidfd", refused_pidfd(process_ids))
    before = open_descriptor_count()

    try:
        with pytest.raises(OSError, match="too many open files"):
            true_child()

        await_absence(process_ids)
    finally:
        reaped_by_process_id(process_ids)
    assert len(process_ids) == 1
    assert open_descriptor_count() == before


def test_a_refused_pidfd_ends_the_life_as_spawn_with_nothing_released(
    monkeypatch: pytest.MonkeyPatch,
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    process_ids: list[int] = []
    monkeypatch.setattr(worker_children, "open_pidfd", refused_pidfd(process_ids))
    spawn = ChainSpawn(programs={"idle": [SLEEPER]})

    try:
        life_end = hand_over(
            connection, serving_with(spawn), accept_time(), listener=listener
        )

        await_absence(process_ids)
    finally:
        reaped_by_process_id(process_ids)
    assert life_end == LifeEnd.SPAWN
    assert spawn.children == []


def test_closing_a_child_closes_its_pidfd_once_and_leaves_no_descriptor_behind() -> (
    None
):
    before = open_descriptor_count()
    child = true_child()
    exit_descriptor = child.exit_descriptor

    end_child(child)

    assert is_closed(exit_descriptor)
    assert open_descriptor_count() == before


@pytest.mark.parametrize("path", ["a gate mismatch", "an expiry", "an exit", "a raise"])
def test_the_controller_closes_the_pidfd_on_every_path(
    path: str,
    connection: socket.socket,
    listener: socket.socket,
) -> None:
    program = EXITING_SEVEN if path == "an exit" else SLEEPER
    spawn = ChainSpawn(programs={"idle": [program]})
    serving = serving_with(spawn, admits=path != "a gate mismatch")
    accepted_at = accept_time()
    if path == "an expiry":
        accepted_at -= HEADER_SECONDS - SHORT_SECONDS
    if path == "a raise":
        serving = replace(serving, gate_admits=raising_gate)
    try:
        if path == "a raise":
            with pytest.raises(GateCheckError):
                hand_over(connection, serving, accepted_at, listener=listener)
        else:
            hand_over(connection, serving, accepted_at, listener=listener)
        [child] = spawn.children
        assert is_closed(child.exit_descriptor)
    finally:
        spawn.kill_and_reap()


class GateCheckError(Exception):
    """The gate check broke in a way the controller does not expect."""


def raising_gate(_status: str) -> bool:
    raise GateCheckError


def test_readiness_returns_the_ready_descriptor_and_none_for_a_quiet_one() -> None:
    quiet_read, quiet_write = os.pipe()
    ready_read, ready_write = os.pipe()
    try:
        os.write(ready_write, b"x")
        started = time.monotonic()

        ready = readable_within([quiet_read, ready_read], SHORT_SECONDS)
        nothing_ready = readable_within([quiet_read], SHORT_SECONDS)
        waited = time.monotonic() - started
    finally:
        for descriptor in (quiet_read, quiet_write, ready_read, ready_write):
            os.close(descriptor)

    assert ready == {ready_read}
    assert nothing_ready == frozenset()
    assert waited >= SHORT_SECONDS


def test_the_real_libc_refuses_a_pidfd_for_no_process_with_its_error() -> None:
    with pytest.raises(OSError, match="Invalid argument") as refusal:
        worker_children.open_pidfd(-1)

    assert refusal.value.errno == errno.EINVAL
