"""Spawns the build worker's children behind a gate shell that waits for a release."""

import os
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


class GatedChild:
    """A started gate shell that holds its program until it is released."""

    def __init__(self, process: "subprocess.Popen[bytes]", gate_write: int) -> None:
        """Take over the process and the write end of its gate pipe."""
        self._process = process
        self._gate_write = gate_write

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

    def close_gate(self) -> None:
        """Close the write end of the gate; the caller closes it once."""
        os.close(self._gate_write)


def spawn_gated(
    command: Sequence[str],
    *,
    identity: ChildIdentity,
    environment: Mapping[str, str],
    pass_fds: Sequence[int],
) -> GatedChild:
    """Spawn the command as the identity behind the gate; the caller releases it."""
    gate_read, gate_write = os.pipe()
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-c", GATE_SCRIPT, *command],
            stdin=gate_read,
            pass_fds=tuple(pass_fds),
            cwd=CHILD_WORKING_DIRECTORY,
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
    return GatedChild(process, gate_write)
