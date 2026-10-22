"""Drive the sealed build worker through its real socket, from the instance image.

Usage: probe_build_worker.py drive
       probe_build_worker.py client COMMAND [ARGUMENT...]

`drive` runs on the machine that has Docker and the project's compose
environment (COMPOSE_PROJECT_NAME and the variables compose.yaml requires). It
starts the worker service from the already built image `presentator`, holds its
socket volume in a container of the runtime user, and runs `client` in that
container, over the real socket, through the codec's client half. Every cell it
drives prints one line, and a cell that is not driven fails the run.
"""

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

from presentator.adapters.build_wire import (
    LENGTH,
    MAGIC,
    VERSION,
    BuiltOutcome,
    MessageKind,
    RecordTag,
    StepHeader,
    WireRefusedError,
    read_outcome,
    read_status,
    receive_entries,
    send_hello,
    send_step,
    send_tree,
)
from presentator.contracts.decks import StepKind

REPOSITORY = Path(__file__).parents[1]
IMAGE = "presentator"
SERVICE = "build-worker"
HOLDER = "build-worker-probe-holder"
SOCKET_DIRECTORY = "/run/presentator-build"
SOCKET_PATH = f"{SOCKET_DIRECTORY}/worker.sock"
PROBE_DIRECTORY = "/probe"
DECKS_DIRECTORY = "/decks"
HELLO_DECK = f"{DECKS_DIRECTORY}/hello-deck"
OUTPUT_DIRECTORY = Path("/out")
FOREIGN_USER = "4242:presentator"
CONNECT_WINDOW_SECONDS = 70
FIRST_RETRY_SECONDS = 0.25
LONGEST_RETRY_SECONDS = 4
ATTEMPT_SECONDS = 30
EXCHANGE_SECONDS = 900
CLOSE_SECONDS = 10
STOP_SECONDS = 2
OUTPUT_MEGABYTES = 300
FAST_LIVES = 3
GLIBC_FLOOR = (2, 36)
REFUSAL_EXIT_STATUS = 2
BUILD_VARIABLES = (
    "PRESENTATOR_BUILD_TIMEOUT_SECONDS",
    "PRESENTATOR_BUILD_OUTPUT_MEGABYTES",
)
PREAMBLE = MAGIC + bytes([VERSION, MessageKind.STEP])
VALID_STEP = b'{"step":"build","slug":"a"}'


def _framed(body: bytes, length: int | None = None) -> bytes:
    return LENGTH.pack(len(body) if length is None else length) + body


HEADER_CLASSES = {
    "length 0": _framed(b"", 0),
    "length 1": _framed(b"{"),
    "length 4097": _framed(b"", 4097),
    "non-UTF-8": _framed(b'{"step":"build","slug":"\xff\xff"}'),
    "extra key": _framed(b'{"step":"build","slug":"a","extra":1}'),
    "wrong type": _framed(b'{"step":1,"slug":"a"}'),
    "unknown step": _framed(b'{"step":"deploy","slug":"a"}'),
    "slug not a plain folder name": _framed(b'{"step":"build","slug":"../a"}'),
    "dripped past the header deadline": _framed(VALID_STEP[:9], len(VALID_STEP)),
}
CELLS = ("5", "6", "48", "49", "51", "52", "54", "D1", "D2", "D4", "D5", "D6")


def refuse(claim: str) -> NoReturn:
    """Stop with the claim that did not hold."""
    sys.stderr.write(f"probe failed: {claim}\n")
    sys.exit(1)


def say(line: str) -> None:
    """Print one line and flush it, so a hung probe shows where it stopped."""
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


# The codec's client half, run inside the holder container as the runtime user.


def connect(timeout: float) -> socket.socket:
    """Connect to the worker's socket; raise OSError while it is not served."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(SOCKET_PATH)
    except OSError:
        connection.close()
        raise
    return connection


def attempt(opening: Callable[[socket.socket], None]) -> socket.socket | None:
    """One connect, opening message and STATUS; None when the worker is not up."""
    try:
        connection = connect(ATTEMPT_SECONDS)
    except OSError:
        return None
    try:
        opening(connection)
        read_status(connection)
    except (OSError, WireRefusedError):
        connection.close()
        return None
    connection.settimeout(EXCHANGE_SECONDS)
    return connection


def open_exchange(opening: Callable[[socket.socket], None]) -> socket.socket:
    """Retry the opening within the connect window until STATUS arrives."""
    deadline = time.monotonic() + CONNECT_WINDOW_SECONDS
    delay = FIRST_RETRY_SECONDS
    while (connection := attempt(opening)) is None:
        if time.monotonic() + delay > deadline:
            refuse("no STATUS within the connect window")
        time.sleep(delay)
        delay = min(delay * 2, LONGEST_RETRY_SECONDS)
    return connection


def client_hello() -> None:
    """Wait for STATUS to a HELLO and print how long it took."""
    started = time.monotonic()
    open_exchange(send_hello).close()
    say(f"status after {(time.monotonic() - started) * 1000:.0f} ms")


def expect_closed_without_a_reply(connection: socket.socket) -> None:
    """Wait for the worker to close the connection having written nothing."""
    try:
        reply = connection.recv(1)
    except ConnectionResetError:
        reply = b""
    except TimeoutError:
        refuse("the connection stayed open")
    if reply:
        refuse("the worker answered a connection it must close")
    say("closed, nothing written")


def client_closed_at_accept() -> None:
    """A HELLO from a peer that is not the runtime user is closed at accept."""
    connection = connect(CLOSE_SECONDS)
    try:
        send_hello(connection)
    except BrokenPipeError:
        say("closed, nothing written")
    else:
        expect_closed_without_a_reply(connection)
    connection.close()


def client_header_class(name: str) -> None:
    """A malformed STEP header is closed with nothing written."""
    connection = connect(CLOSE_SECONDS)
    connection.sendall(PREAMBLE + HEADER_CLASSES[name])
    expect_closed_without_a_reply(connection)
    connection.close()


def client_bad_upload() -> None:
    """A STEP whose upload breaks the path rules ends the life with no reply."""
    connection = open_exchange(
        lambda opened: send_step(opened, StepHeader(step=StepKind.BUILD, slug="a"))
    )
    path = b"/absolute"
    connection.sendall(bytes([RecordTag.DIR]) + LENGTH.pack(len(path)) + path)
    expect_closed_without_a_reply(connection)
    connection.close()


def client_hold() -> None:
    """Take a step's STATUS and hold the upload open until the worker is stopped."""
    connection = open_exchange(
        lambda opened: send_step(opened, StepHeader(step=StepKind.BUILD, slug="a"))
    )
    say("status")
    connection.recv(1)
    connection.close()


def client_step(slug: str, step: StepKind) -> None:
    """Upload the deck, read the outcome and receive the output below /out."""
    header = StepHeader(step=step, slug=slug)
    connection = open_exchange(lambda opened: send_step(opened, header))
    deck = os.open(Path(DECKS_DIRECTORY) / slug, os.O_RDONLY | os.O_DIRECTORY)
    received = OUTPUT_DIRECTORY / f"{step}-{slug}"
    received.mkdir()
    output = os.open(received, os.O_RDONLY | os.O_DIRECTORY)
    try:
        send_tree(connection, deck)
        outcome = read_outcome(connection)
        if not isinstance(outcome, BuiltOutcome):
            refuse(f"{step} of {slug} answered {outcome}")
        receive_entries(connection, output, OUTPUT_MEGABYTES)
    finally:
        os.close(deck)
        os.close(output)
        connection.close()
    say(f"{step} of {slug} built and received")


def client(command: str, arguments: list[str]) -> None:
    """Run one client command."""
    match command, arguments:
        case "hello", []:
            client_hello()
        case "closed-at-accept", []:
            client_closed_at_accept()
        case "header-class", [name]:
            client_header_class(name)
        case "bad-upload", []:
            client_bad_upload()
        case "hold", []:
            client_hold()
        case "step", [slug, step]:
            client_step(slug, StepKind(step))
        case _:
            sys.exit(__doc__)


# The driver, run where Docker is.


def docker(*arguments: str) -> str:
    """Run the docker CLI, failing loudly, and return what it printed."""
    return subprocess.run(
        ("docker", *arguments), check=True, capture_output=True, text=True
    ).stdout.strip()


def compose(*arguments: str) -> str:
    """Run docker compose in the repository, failing loudly."""
    return docker("compose", "--file", str(REPOSITORY / "compose.yaml"), *arguments)


def in_holder(
    *command: str, user: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in the holder container; the caller judges the status."""
    who = ("--user", user) if user else ()
    return subprocess.run(
        ("docker", "exec", *who, HOLDER, *command),
        check=False,
        capture_output=True,
        text=True,
    )


def probe_client(*arguments: str, user: str | None = None) -> str:
    """Run a client command in the holder, failing with its error."""
    done = in_holder(
        "python",
        f"{PROBE_DIRECTORY}/{Path(__file__).name}",
        "client",
        *arguments,
        user=user,
    )
    if done.returncode:
        refuse(f"client {' '.join(arguments)}: {done.stdout}{done.stderr}")
    return done.stdout.strip()


def state() -> tuple[str, str]:
    """The worker container's start time and restart count."""
    started, restarts = docker(
        "inspect", "--format", "{{.State.StartedAt}} {{.RestartCount}}", worker()
    ).split()
    return started, restarts


def worker() -> str:
    """The id of the worker container."""
    return compose("ps", "--all", "--quiet", SERVICE)


def timed(action: Callable[[], object]) -> float:
    """The seconds the action took."""
    started = time.monotonic()
    action()
    return time.monotonic() - started


class Report:
    """The cells driven so far, one printed line each."""

    def __init__(self) -> None:
        """Start with no cell driven."""
        self.driven: set[str] = set()

    def cell(self, name: str, line: str) -> None:
        """Record a cell as driven and print its line."""
        self.driven.add(name)
        say(f"cell {name}: {line}")

    def finish(self) -> None:
        """Fail unless every cell was driven."""
        missing = [name for name in CELLS if name not in self.driven]
        if missing:
            refuse(f"cells not driven: {', '.join(missing)}")


def prove_variables_reach_the_worker(report: Report) -> None:
    """D5: compose gives the worker both build variables by name, or neither."""
    values = dict.fromkeys(BUILD_VARIABLES, "7")
    with tempfile.TemporaryDirectory() as project:
        for given in (values, {}):
            Path(project, ".env").write_text(
                "".join(f"{name}={value}\n" for name, value in given.items()),
                encoding="utf-8",
            )
            configured = json.loads(
                compose(
                    "--project-directory",
                    project,
                    "config",
                    "--format",
                    "json",
                    SERVICE,
                )
            )["services"][SERVICE]["environment"]
            resolved = {name: configured.get(name) for name in BUILD_VARIABLES}
            expected = {name: given.get(name) for name in BUILD_VARIABLES}
            if resolved != expected:
                refuse(f"compose resolved {resolved}, expected {expected}")
    report.cell("D5", "both build variables reach the worker from .env, or neither")


def prove_glibc_floor(report: Report) -> None:
    """D6: the image's glibc has pidfd_open."""
    version = docker("run", "--rm", IMAGE, "getconf", "GNU_LIBC_VERSION").split()[-1]
    if tuple(int(part) for part in version.split(".")) < GLIBC_FLOOR:
        refuse(f"glibc {version} is older than the floor")
    report.cell("D6", f"glibc {version}")


def prove_unsealed_refusal_and_sealed_check(report: Report) -> None:
    """Cell 52: refused in an unsealed container, exit 0 in the sealed service."""
    unsealed = subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--user",
            "0:presentator",
            IMAGE,
            "presentator-worker",
            "--check",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if (
        unsealed.returncode != REFUSAL_EXIT_STATUS
        or "start refused: capabilities" not in unsealed.stderr
    ):
        refuse(f"unsealed --check: {unsealed.returncode} {unsealed.stderr}")
    sealed = subprocess.run(
        (
            "docker",
            "compose",
            "--file",
            str(REPOSITORY / "compose.yaml"),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            SERVICE,
            "presentator-worker",
            "--check",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if sealed.returncode:
        refuse(f"sealed --check exited {sealed.returncode}: {sealed.stderr}")
    report.cell(
        "52", "unsealed refused naming capabilities, exit 2; sealed --check exit 0"
    )


def prove_inspect_sealing(report: Report) -> None:
    """Cell 51: the configuration Docker applied, and the daemon's own sealing."""
    container = json.loads(docker("inspect", worker()))[0]
    host = container["HostConfig"]
    mounts = {mount["Target"]: mount for mount in host["Mounts"]}
    held = {
        "SecurityOpt": host["SecurityOpt"] == ["no-new-privileges"],
        "AppArmorProfile": container["AppArmorProfile"] == "docker-default",
        "CapAdd": sorted(host["CapAdd"]) == ["CAP_KILL", "CAP_SETGID", "CAP_SETUID"],
        "CapDrop": host["CapDrop"] == ["ALL"],
        "ReadonlyRootfs": host["ReadonlyRootfs"] is True,
        "NetworkMode": host["NetworkMode"] == "none",
        "IpcMode": host["IpcMode"] == "private",
        "PidMode": host["PidMode"] == "",
        "Privileged": host["Privileged"] is False,
        "MemorySwap": host["MemorySwap"] == host["Memory"] > 0,
        "Init": not host.get("Init"),
        "WorkTmpfs": "mode=755" in host["Tmpfs"]["/work"].split(","),
        "NoCopy": mounts[SOCKET_DIRECTORY]["VolumeOptions"]["NoCopy"] is True,
        "Seccomp": "name=seccomp,profile=builtin"
        in docker("info", "--format", "{{json .SecurityOptions}}"),
        "Confinement": docker("exec", worker(), "cat", "/proc/1/attr/current")
        == "docker-default (enforce)",
    }
    failed = [name for name, ok in held.items() if not ok]
    if failed:
        refuse(f"inspect: {', '.join(failed)} in {json.dumps(host)}")
    report.cell("51", f"{len(held)} sealing facts held")


def socket_facts() -> tuple[str, str]:
    """The socket directory's and the socket's mode, owner and group."""
    directory = in_holder("stat", "-c", "%a %U:%G", SOCKET_DIRECTORY).stdout.strip()
    socket_file = in_holder("stat", "-c", "%a %U:%G %F", SOCKET_PATH).stdout.strip()
    return directory, socket_file


def prove_socket_identity(report: Report, name: str) -> None:
    """Cell 5: after bind the socket is 0660 root:presentator in a 0750 directory."""
    directory, socket_file = socket_facts()
    if (directory, socket_file) != (
        "750 root:presentator",
        "660 root:presentator socket",
    ):
        refuse(f"socket identity: {directory}; {socket_file}")
    report.cell(name, f"directory {directory}; socket {socket_file}")


def start_holder() -> None:
    """Hold the socket volume in a runtime-user container, as the web will."""
    volume = f"{os.environ['COMPOSE_PROJECT_NAME']}_build-socket"
    docker(
        "run", "--detach", "--name", HOLDER, "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--user", "presentator",
        "--tmpfs", f"{OUTPUT_DIRECTORY}:mode=1777,size=512m",
        "--volume", f"{volume}:{SOCKET_DIRECTORY}",
        "--volume", f"{Path(__file__).resolve().parent}:{PROBE_DIRECTORY}:ro",
        "--volume", f"{REPOSITORY / 'examples' / 'hello-deck'}:{HELLO_DECK}:ro",
        "--volume",
        f"{Path(__file__).resolve().parent / 'probe_build_worker' / 'render'}"
        f":{DECKS_DIRECTORY}/render:ro",
        IMAGE, "sleep", "infinity",
    )  # fmt: skip


def prove_fresh_volume(report: Report) -> None:
    """D2: a fresh root is private, so no entry can be planted before the worker."""
    facts = in_holder("stat", "-c", "%a %u %g", SOCKET_DIRECTORY).stdout.strip()
    planted = in_holder("touch", f"{SOCKET_DIRECTORY}/planted")
    if facts != "750 0 0" or "Permission denied" not in planted.stderr:
        refuse(f"fresh volume: {facts}; {planted.returncode} {planted.stderr}")
    report.cell("D2", "fresh root 750 0 0; the runtime user cannot create an entry")


def prove_refused_at_accept(report: Report) -> None:
    """Cell 6: uid 0 and another uid of the group are closed at accept."""
    before = state()
    for user in ("0", FOREIGN_USER):
        probe_client("closed-at-accept", user=user)
    probe_client("hello")
    if state() != before:
        refuse("the worker restarted")
    report.cell(
        "6", "uid 0 and a foreign uid closed at accept; no restart, STATUS answered"
    )


def prove_header_classes(report: Report) -> None:
    """Cell 49: each header class is closed, and the same container answers STATUS."""
    before = state()
    for name in HEADER_CLASSES:
        probe_client("header-class", name)
        probe_client("hello")
    if state() != before:
        refuse("a header class restarted the worker")
    report.cell(
        "49",
        f"{len(HEADER_CLASSES)} header classes closed; no restart; "
        f"STATUS {probe_client('hello')}",
    )


def extract(source: str, destination: Path) -> None:
    """Copy a directory of the holder out through tar, since tmpfs defeats docker cp."""
    packed = subprocess.run(
        ("docker", "exec", HOLDER, "tar", "--create", "--directory", source, "."),
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
        archive.extractall(destination, filter="data")


def text_files(directory: Path) -> Iterator[str]:
    """The script, markup and page text files below a directory, decoded."""
    for path in directory.rglob("*"):
        if path.suffix in {".js", ".html"}:
            yield path.read_text(encoding="utf-8", errors="replace")


def prove_builds(report: Report) -> None:
    """Cells 54 and D4: hello-deck and the render deck built and exported."""
    for slug in ("hello-deck", "render"):
        for step in StepKind:
            probe_client("step", slug, step)
    with tempfile.TemporaryDirectory() as received:
        output = Path(received)
        extract(str(OUTPUT_DIRECTORY), output)
        for slug in ("hello-deck", "render"):
            talk = output / f"build-{slug}" / "talk"
            pdf = output / f"export-{slug}" / "deck.pdf"
            if not (talk / "index.html").is_file() or not pdf.read_bytes().startswith(
                b"%PDF-"
            ):
                refuse(f"{slug}: the talk's page or the PDF is missing")
        if not any(
            "katex-mathml" in text for text in text_files(output / "build-render")
        ):
            refuse("the built render talk carries no KaTeX markup")
        words = subprocess.run(
            ("pdftotext", str(output / "export-render" / "deck.pdf"), "-"),
            check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip
        label = "Probe node alpha" in words
    report.cell("54", "hello-deck built and exported through the real worker")
    report.cell(
        "D4", f"KaTeX markup in the talk; mermaid label in the PDF text: {label}"
    )


def prove_fast_lives(report: Report) -> None:
    """Cell 48: a fast life leaves a stale socket that the next life replaces."""
    delays: list[str] = []
    for _ in range(FAST_LIVES):
        probe_client("bad-upload")
        delays.append(f"{timed(lambda: probe_client('hello')):.2f}")
    prove_socket_identity(report, "48")
    say(f"cell 48: restart delays in seconds {delays}")


def prove_stop(report: Report) -> None:
    """D1: SIGKILL stops the worker in under two seconds, idle and during a step."""
    seconds: list[float] = []
    for during_a_step in (False, True):
        held = None
        if during_a_step:
            held = subprocess.Popen(
                ("docker", "exec", HOLDER, "python",
                 f"{PROBE_DIRECTORY}/{Path(__file__).name}", "client", "hold"),
                stdout=subprocess.PIPE, text=True,
            )  # fmt: skip
            if held.stdout is None or held.stdout.readline().strip() != "status":
                refuse("the held step took no STATUS")
        seconds.append(timed(lambda: compose("stop", SERVICE)))
        if held is not None:
            held.kill()
            held.wait()
        if in_holder("test", "-S", SOCKET_PATH).returncode:
            refuse("a stopped worker left no stale socket to replace")
        compose("up", "--detach", "--no-build", SERVICE)
        probe_client("hello")
    if max(seconds) >= STOP_SECONDS:
        refuse(f"stop took {seconds}")
    report.cell(
        "D1", f"stopped idle and during a step in {seconds}; stale socket replaced"
    )


def drive() -> None:
    """Start the worker from the built image and drive every cell."""
    report = Report()
    prove_variables_reach_the_worker(report)
    prove_glibc_floor(report)
    compose("up", "--no-start", SERVICE)
    try:
        start_holder()
        prove_fresh_volume(report)
        compose("up", "--detach", "--no-build", SERVICE)
        say(f"first STATUS {probe_client('hello')}")
        prove_socket_identity(report, "5")
        prove_inspect_sealing(report)
        prove_unsealed_refusal_and_sealed_check(report)
        prove_refused_at_accept(report)
        prove_header_classes(report)
        prove_builds(report)
        prove_fast_lives(report)
        prove_stop(report)
    finally:
        subprocess.run(
            ("docker", "compose", "--file", str(REPOSITORY / "compose.yaml"),
             "logs", "--no-color", SERVICE),
            check=False,
        )  # fmt: skip
        subprocess.run(("docker", "rm", "--force", HOLDER), check=False)
        subprocess.run(
            ("docker", "compose", "--file", str(REPOSITORY / "compose.yaml"),
             "down", "--volumes"),
            check=False,
        )  # fmt: skip
    report.finish()


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["drive"]:
            drive()
        case ["client", command, *arguments]:
            client(command, arguments)
        case _:
            sys.exit(__doc__)
