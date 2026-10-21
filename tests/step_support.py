"""The build step's doubles: scripted, refusing and fake-pnpm."""

import os
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from presentator.adapters import worker_children
from presentator.adapters.worker_children import (
    ChildIdentity,
    ChildSurroundings,
    GatedChild,
    spawn_gated,
)
from presentator.host.worker_serving import StepEffects, WireChild

if TYPE_CHECKING:
    from tests.worker_support import ScriptedLife

STEP_PROJECT = Path("/test/project")
OWN_IDENTITY = ChildIdentity(os.getuid(), os.getgid(), extra_groups=None)


GATED_STATUS = "Uid:\t10002\t10002\t10002\t10002"


ADMITTING_PATH = "/opt/test/bin"


STEP_LEG = [
    "prepare",
    "step spawn",
    "step status",
    "step release",
    "step readable",
    "step wait",
    "step close",
]


STEP_EXIT_DESCRIPTOR = 1_000_001


STEP_ENVIRONMENT = {
    "PATH": ADMITTING_PATH,
    "HOME": "/test/work/home",
    "TMPDIR": "/test/tmp",
    "PLAYWRIGHT_BROWSERS_PATH": "/test/playwright",
    "COREPACK_HOME": "/test/corepack",
    "COREPACK_ENABLE_NETWORK": "0",
}


@dataclass(frozen=True)
class StepSpawned:
    """What the controller asked the step's spawn for."""

    command: Sequence[str]
    environment: Mapping[str, str]
    pass_fds: Sequence[int]
    surroundings: ChildSurroundings


def _refuse_unused_spawn(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    pass_fds: Sequence[int],
    surroundings: ChildSurroundings,
) -> WireChild:
    message = f"the step is not part of this test: {command} {environment}"
    raise AssertionError(message, pass_fds, surroundings)


def _refuse_unused_prepare(work: Path) -> int:
    message = f"the step is not part of this test: {work}"
    raise AssertionError(message)


UNUSED_STEP = StepEffects(
    spawn=_refuse_unused_spawn,
    gate_admits=lambda _status: False,
    prepare=_refuse_unused_prepare,
    environment={},
    project=STEP_PROJECT,
)


@dataclass
class ScriptedStep:
    """The build step of a scripted life; its events join the life's, prefixed."""

    spawn_error: OSError | None = None
    status: str | OSError = GATED_STATUS
    gate_admits: bool = True
    release_error: OSError | None = None
    exit_status: int = 0
    stalled: bool = False
    prepare_error: OSError | None = None
    prepare_takes: float = 0.0
    spawn_takes: float = 0.0
    inspection_takes: float = 0.0
    wake_takes: float = 0.0
    listener_wakes: int = 0
    connection_wakes: bool = False
    killed: bool = False
    watched: list[frozenset[int]] = field(default_factory=list[frozenset[int]])
    spawned: list[StepSpawned] = field(default_factory=list[StepSpawned])
    life: "ScriptedLife | None" = None

    @property
    def scripted(self) -> "ScriptedLife":
        assert self.life is not None
        return self.life

    def spawn(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int],
        surroundings: ChildSurroundings,
    ) -> "ScriptedStep":
        self.scripted.events.append("step spawn")
        self.spawned.append(StepSpawned(command, environment, pass_fds, surroundings))
        self.scripted.clock_now += self.spawn_takes
        if self.spawn_error is not None:
            raise self.spawn_error
        return self

    def admits(self, _status: str) -> bool:
        return self.gate_admits

    def status_text(self) -> str:
        self.scripted.events.append("step status")
        self.scripted.clock_now += self.inspection_takes
        if isinstance(self.status, OSError):
            raise self.status
        return self.status

    def release(self) -> None:
        self.scripted.events.append("step release")
        if self.release_error is not None:
            raise self.release_error

    def kill(self) -> None:
        self.scripted.events.append("step kill")
        self.killed = True
        if self.scripted.kill_error is not None:
            raise self.scripted.kill_error

    def wait(self) -> int:
        self.scripted.events.append("step wait")
        if self.killed and self.scripted.reap_error is not None:
            raise self.scripted.reap_error
        return self.exit_status

    def wait_within(self, seconds: float) -> int | None:
        message = "the step is waited on through its descriptor"
        raise AssertionError(message)

    @property
    def exit_descriptor(self) -> int:
        return STEP_EXIT_DESCRIPTOR

    def close(self) -> None:
        self.scripted.events.append("step close")

    def woken(self, descriptors: Sequence[int], seconds: float) -> frozenset[int]:
        life = self.scripted
        life.events.append("step readable")
        life.budgets_waited.append(seconds)
        life.raise_wait_error_at_this_wait()
        life.clock_now += self.wake_takes
        self.watched.append(frozenset(descriptors))
        connection = life.spawned[0].pass_fds[0]
        listener = frozenset(descriptors) - {STEP_EXIT_DESCRIPTOR, connection}
        if self.listener_wakes > 0 and listener:
            self.listener_wakes -= 1
            return listener
        if self.connection_wakes:
            return frozenset({connection})
        if self.stalled:
            life.clock_now += seconds
            return frozenset()
        return frozenset({STEP_EXIT_DESCRIPTOR})


def fake_pnpm(directory: Path, program: str) -> Path:
    """A directory holding an executable `pnpm` that runs the Python program."""
    directory.mkdir()
    executable = directory / "pnpm"
    executable.write_text(f"#!{sys.executable}\n{program}")
    executable.chmod(0o755)
    return directory


def announcing(fifo: Path) -> str:
    """Python source that writes the running process's id into the FIFO."""
    return (
        "import os\n"
        f"announced = os.open({str(fifo)!r}, os.O_WRONLY)\n"
        "os.write(announced, str(os.getpid()).encode())\n"
        "os.close(announced)\n"
    )


@contextmanager
def announcements(directory: Path) -> Generator[tuple[Path, int]]:
    """A FIFO, already open for reading, that a fake pnpm announces itself in."""
    fifo = directory / "announced"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        yield fifo, reader
    finally:
        os.close(reader)


def await_running_step(reader: int, seconds: float) -> int:
    """The process id the running fake pnpm announced; the test fails without one."""
    if not worker_children.readable_within((reader,), seconds):
        pytest.fail("the step did not start running")
    return int(os.read(reader, 32))


def quick_step(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    pass_fds: Sequence[int],
    surroundings: ChildSurroundings,
) -> GatedChild:
    """A step that exits 0 at once, whatever the launcher's command is."""
    return spawn_gated(
        ["/bin/true", *command],
        identity=OWN_IDENTITY,
        environment=environment,
        pass_fds=pass_fds,
        surroundings=surroundings,
    )
