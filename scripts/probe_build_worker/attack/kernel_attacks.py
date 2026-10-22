"""The kernel-facing attacks of the sealed worker's isolation probe, in two vehicles.

`deck ONE PROBE` runs as a child of the attack deck's build code (uid 10001) and
attacks PID 1 and the probe. `probe` runs as the exec'd 10002 probe, program on
stdin (`python -I - probe`), and attacks PID 1 and the running step. `fork` and
`ipc` leave what a life must not carry into the next. Every line printed is
`KIND ...`; a check records only the kernel's errno name or `succeeded`, never
what it read.
"""

import contextlib
import ctypes
import errno
import os
import signal
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

# The life's shared 1777 tmpfs and shared memory, by design: the attackers' rendezvous.
RENDEZVOUS = Path("/tmp")  # noqa: S108
SHARED_MEMORY = Path("/dev/shm")  # noqa: S108
WORK_HOME = Path("/work/home")
WAIT_SECONDS = 60
POLL_SECONDS = 0.1
SOCKET_DIRECTORY = "/run/presentator-build"
WORKER_SOCKET = f"{SOCKET_DIRECTORY}/worker.sock"
PTRACE_ATTACH = 16
SYS_KEYCTL = 250
KEYCTL_GET_KEYRING_ID = 0
KEY_SPEC_SESSION_KEYRING = -3
CLONE_NEWUSER = 0x10000000
IPC_CREAT = 0o1000
IPC_PRIVATE = 0
SHARED_BYTES = 4096
OWNER_MODE = 0o600
STATUS_FIELDS = (
    "Uid",
    "Gid",
    "Groups",
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapAmb",
    "NoNewPrivs",
    "Seccomp",
)
LIFETIME_SECONDS = 600
SIGNALS = (signal.SIGSTOP, signal.SIGKILL)

libc = ctypes.CDLL(None, use_errno=True)


def emit(line: str) -> None:
    """Print one line and flush it."""
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def check(key: str, attack: Callable[[], object]) -> None:
    """Record the errno name the attack raised, or `succeeded`."""
    try:
        attack()
    except OSError as refusal:
        emit(f"check {key} {errno.errorcode.get(refusal.errno or 0, 'unknown')}")
    else:
        emit(f"check {key} succeeded")


def failing_call(result: int) -> None:
    """Raise the errno of a failed libc call."""
    if result == -1:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def report_status(who: str) -> None:
    """Print this process's own identity fields, the gate every count opens with."""
    for line in Path("/proc/self/status").read_text().splitlines():
        name, _, values = line.partition(":")
        if name in STATUS_FIELDS:
            emit(f"status {who} {name} {values.strip()}")


def report_confinement() -> None:
    """Print the AppArmor profile and whether this process shares PID 1's cgroup."""
    try:
        profile = Path("/proc/self/attr/current").read_text().strip("\n\x00")
    except OSError as refusal:
        profile = f"unreadable-{errno.errorcode.get(refusal.errno or 0, 'unknown')}"
    emit(f"profile {profile}")
    same = Path("/proc/self/cgroup").read_text() == Path("/proc/1/cgroup").read_text()
    emit(f"cgroup {'same-as-pid-1' if same else 'differs-from-pid-1'}")


def published(name: str) -> str | None:
    """What the other side published under that name, or None."""
    try:
        return (RENDEZVOUS / name).read_text().strip() or None
    except OSError:
        return None


def publish(name: str, content: str) -> None:
    """Publish atomically, readable by the other uid."""
    pending = RENDEZVOUS / f".{name}.{os.getpid()}"
    pending.write_text(content)
    pending.rename(RENDEZVOUS / name)


def wait_for(name: str) -> str | None:
    """Wait a bounded time for the other side's file; None records `not driven`."""
    deadline = time.monotonic() + WAIT_SECONDS
    while (content := published(name)) is None:
        if time.monotonic() > deadline:
            emit(f"not-driven waited for {name}")
            return None
        time.sleep(POLL_SECONDS)
    return content


def kill_check(label: str, target: int) -> None:
    """Signal the target with each signal a cross-uid kill must be refused."""
    for number in SIGNALS:
        check(f"kill.{label}.{number.name}", lambda n=number: os.kill(target, n))


def ptrace_check(label: str, target: int) -> None:
    """Attach to the target; Yama and the uid rule both refuse it."""
    libc.ptrace.restype = ctypes.c_long
    check(
        f"ptrace.{label}",
        lambda: failing_call(libc.ptrace(PTRACE_ATTACH, target, None, None)),
    )


def read_check(key: str, path: str) -> None:
    """Open the file for reading and close it again."""
    check(key, lambda: os.close(os.open(path, os.O_RDONLY)))


def socket_checks() -> None:
    """Bind and connect in the controller's socket directory."""

    def bind() -> None:
        with socket.socket(socket.AF_UNIX) as unbound:
            unbound.bind(f"{SOCKET_DIRECTORY}/attack.sock")

    def connect() -> None:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(WORKER_SOCKET)

    check("bind.socket-directory", bind)
    check("connect.worker-socket", connect)


def create_check(key: str, directory: str) -> None:
    """Create a file below the directory; it must never exist."""
    check(
        key,
        lambda: os.close(
            os.open(
                f"{directory}/attack-marker",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                OWNER_MODE,
            )
        ),
    )


def write_marker(path: str) -> None:
    """Leave a marker the next life must not carry, and report where."""
    try:
        Path(path).write_text("marker")
    except OSError as refusal:
        emit(f"check marker.{path} {errno.errorcode.get(refusal.errno or 0)}")
    else:
        emit(f"marker {path}")


def owner_of(process: int) -> str:
    """The real uid of a live process, from its status."""
    for line in Path(f"/proc/{process}/status").read_text().splitlines():
        if line.startswith("Uid:"):
            return line.split()[1]
    return "unknown"


def double_fork() -> None:
    """Leave a `setsid` double-forked child that outlives this process."""
    read, write = os.pipe()
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            os.write(write, str(os.getpid()).encode())
            os.close(write)
            null = os.open(os.devnull, os.O_RDWR)
            for descriptor in range(3):
                os.dup2(null, descriptor)
            time.sleep(LIFETIME_SECONDS)
        # The middle child must not run the parent's exit handlers.
        os._exit(0)
    os.close(write)
    with os.fdopen(read) as pipe:
        grandchild = int(pipe.read())
    os.wait()
    emit(f"child {grandchild} {owner_of(grandchild)}")


def sysv_checks() -> None:
    """Create a SysV shared memory segment, message queue and semaphore set."""
    flags = IPC_CREAT | OWNER_MODE
    check(
        "ipc.shmget",
        lambda: failing_call(libc.shmget(IPC_PRIVATE, SHARED_BYTES, flags)),
    )
    check("ipc.msgget", lambda: failing_call(libc.msgget(IPC_PRIVATE, flags)))
    check("ipc.semget", lambda: failing_call(libc.semget(IPC_PRIVATE, 1, flags)))


def deck_attacks(one: int, probe: int) -> None:
    """The ctypes half of the deck's attacks: ptrace, /proc, keyctl, unshare."""
    report_status("python")
    ptrace_check("1", one)
    ptrace_check("probe", probe)
    read_check("read.proc-1-environ", f"/proc/{one}/environ")
    read_check("read.proc-1-mem", f"/proc/{one}/mem")
    check(
        "keyctl",
        lambda: failing_call(
            libc.syscall(SYS_KEYCTL, KEYCTL_GET_KEYRING_ID, KEY_SPEC_SESSION_KEYRING, 0)
        ),
    )
    check("unshare.user", lambda: failing_call(libc.unshare(CLONE_NEWUSER)))
    sysv_checks()


def probe_attacks() -> None:
    """Everything the exec'd 10002 probe does, in the rendezvous order."""
    report_status("probe")
    report_confinement()
    if (deck := wait_for("deck.pid")) is None:
        return
    step = int(deck)
    publish("probe.pid", str(os.getpid()))
    for label, target in (("1", 1), ("step", step)):
        kill_check(label, target)
        ptrace_check(label, target)
    read_check("read.proc-1-environ", "/proc/1/environ")
    read_check("read.proc-1-mem", "/proc/1/mem")
    read_check("read.step-mem", f"/proc/{step}/mem")
    socket_checks()
    for key, directory in (
        ("create.root", "/"),
        ("create.opt", "/opt"),
        ("create.frontend", "/app/frontend"),
        ("create.work", "/work"),
    ):
        create_check(key, directory)
    check("open.step-err", lambda: os.close(os.open("/work/step.err", os.O_WRONLY)))
    for directory in (RENDEZVOUS, SHARED_MEMORY, WORK_HOME):
        write_marker(str(directory / "probe-marker"))
    double_fork()
    emit("complete")
    publish("probe.done", "done")
    wait_for("deck.done")


def main(arguments: list[str]) -> None:
    """Run one vehicle's share."""
    match arguments:
        case ["deck", one, probe]:
            deck_attacks(int(one), int(probe))
        case ["probe"]:
            probe_attacks()
        case ["fork"]:
            double_fork()
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        main(sys.argv[1:])
