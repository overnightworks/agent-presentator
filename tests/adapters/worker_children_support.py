"""Shared arrangement for the gated child's tests."""

import time
from collections.abc import Callable
from pathlib import Path

from presentator.adapters import worker_children
from presentator.host import worker
from presentator.host.worker_serving import Serving, WireChild
from tests.worker_support import NeverArmed, nothing


def serving_with(
    spawn: Callable[..., WireChild], *, admits: bool = True, quiet: bool = True
) -> Serving:
    return Serving(
        spawn=spawn,
        gate_admits=lambda _status: admits,
        sweep=lambda _deadline: quiet,
        create_record=worker.create_request_record,
        seal=worker.seal_record,
        open_deck=nothing,
        close_deck=nothing,
        watchdog=NeverArmed(),
        clock=time.monotonic,
        readable=worker_children.readable_within,
        build_timeout_seconds=300.0,
    )


def accept_time() -> float:
    return time.monotonic()


def open_descriptor_count() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))
