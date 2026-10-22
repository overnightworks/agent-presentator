"""Spawns the build worker's children behind a gate shell that waits for a release."""

import ctypes
import math
import os
import select
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

GATE_SCRIPT = (
    'echo 1000 >/proc/self/oom_score_adj && read -r _ && exec "$0" "$@" </dev/null'
)
RELEASE = b"go\n"
CHILD_WORKING_DIRECTORY = "/"
CHILD_UMASK = 0o022


@dataclass(frozen=True)
class ChildIdentity:
    """The ids a child is switched to before its program runs."""

    uid: int
    gid: int
    extra_groups: tuple[int, ...] | None


@dataclass(frozen=True)
class ChildSurroundings:
    """Where a child runs and where its output goes; by default its parent's streams."""

    cwd: Path = Path(CHILD_WORKING_DIRECTORY)
    discard_stdout: bool = False
    stderr: int | None = None


INHERITED_SURROUNDINGS = ChildSurroundings()


class GatedChild:
    """A started gate shell that holds its program until it is released."""

    def __init__(
        self,
        process: "subprocess.Popen[bytes]",
        gate_write: int,
        exit_descriptor: int,
    ) -> None:
        """Take over the process, its gate pipe's write end and its pidfd."""
        self._process = process
        self._gate_write = gate_write
        self.exit_descriptor = exit_descriptor

    def status_text(self) -> str:
        """Read the kernel's account of the child; an unreadable one raises."""
        return Path(f"/proc/{self._process.pid}/status").read_text()

    def release(self) -> None:
        """Let the program exec; a dead child raises BrokenPipeError."""
        os.write(self._gate_write, RELEASE)

    def kill(self) -> None:
        """Kill the child, whatever it is running."""
        self._process.kill()

    def wait(self) -> int:
        """Wait for the child and return its exit status (negative for a signal)."""
        return self._process.wait()

    def wait_within(self, seconds: float) -> int | None:
        """Wait at most the seconds; None when the child is still running."""
        try:
            return self._process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        """Close the gate's write end, then the pidfd; the caller closes once."""
        try:
            os.close(self._gate_write)
        finally:
            os.close(self.exit_descriptor)


def readable_within(descriptors: Sequence[int], seconds: float) -> frozenset[int]:
    """The descriptors that are readable within the seconds; none when it runs out."""
    poller = select.poll()
    for descriptor in descriptors:
        poller.register(descriptor, select.POLLIN)
    return frozenset(
        descriptor
        for descriptor, _events in poller.poll(math.ceil(max(seconds, 0.0) * 1000))
    )


def open_pidfd(process_id: int) -> int:
    """A close-on-exec pidfd; the standalone Python has no os.pidfd_open."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.pidfd_open.restype = ctypes.c_int
    descriptor = libc.pidfd_open(process_id, 0)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor


def _gated_child(process: "subprocess.Popen[bytes]", gate_write: int) -> GatedChild:
    try:
        exit_descriptor = open_pidfd(process.pid)
    except BaseException:
        try:
            process.kill()
            process.wait()
        finally:
            os.close(gate_write)
        raise
    return GatedChild(process, gate_write, exit_descriptor)


def spawn_gated(
    command: Sequence[str],
    *,
    identity: ChildIdentity,
    environment: Mapping[str, str],
    pass_fds: Sequence[int],
    surroundings: ChildSurroundings = INHERITED_SURROUNDINGS,
) -> GatedChild:
    """Spawn the command as the identity behind the gate; the caller releases it."""
    gate_read, gate_write = os.pipe()
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-c", GATE_SCRIPT, *command],
            stdin=gate_read,
            stdout=subprocess.DEVNULL if surroundings.discard_stdout else None,
            stderr=surroundings.stderr,
            pass_fds=tuple(pass_fds),
            cwd=surroundings.cwd,
            env=dict(environment),
            umask=CHILD_UMASK,
            start_new_session=True,
            user=identity.uid,
            group=identity.gid,
            extra_groups=(
                None if identity.extra_groups is None else list(identity.extra_groups)
            ),
        )
    except BaseException:
        os.close(gate_write)
        raise
    finally:
        os.close(gate_read)
    return _gated_child(process, gate_write)
