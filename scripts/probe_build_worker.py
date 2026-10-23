"""Drive the sealed build worker through its real socket, from the instance image.

Usage: probe_build_worker.py drive
       probe_build_worker.py client COMMAND [ARGUMENT...]

`drive` runs on the machine that has Docker and the project's compose
environment (COMPOSE_PROJECT_NAME and the variables compose.yaml requires). It
starts the worker service from the already built image `presentator`, holds its
socket volume in a container of the runtime user, and runs `client` in that
container, over the real socket, through the codec's client half. Every cell it
drives prints one line, and a cell that is not driven fails the run.

The life-ending cells (`probe_build_worker_lives.py`) end lives by abandonment,
by exhaustion with the flood deck (`probe_build_worker/flood`), and by a leftover
of a kept life; the client half below reports the outcome a flooded step saw and
abandons a build on the driver's signal.

The refused-upload lives (`probe_build_worker_refusals.py`) send each crafted
upload of `probe_build_worker_uploads.py` in a life of its own while a root
watcher (`probe_build_worker/upload_watcher.py`, through `sudo -n`) records every
create in the worker's writable mounts.

The isolation cells run one life attacked from two sides at once, by the attack
deck (`probe_build_worker/attack`, build code as the build user) and by the 10002
probe (`attack/kernel_attacks.py` on stdin); `probe_build_worker_isolation.py`
drives and judges them.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from probe_build_worker_harness import (
    ENTRY_FILE,
    HOLDER,
    OUTPUT_DIRECTORY,
    PROBE_DIRECTORY,
    PROBE_SOURCES,
    REPOSITORY,
    SERVICE,
    Report,
    compose,
    docker,
    extract,
    in_holder,
    probe_client,
    refuse,
    say,
    state,
    text_files,
    worker,
)
from probe_build_worker_isolation import prove_attacked_life
from probe_build_worker_lives import (
    FLOOD_SLUG,
    Abandonment,
    StepEnd,
    prove_lives_that_end_badly,
)
from probe_build_worker_refusals import prove_refused_uploads
from probe_build_worker_uploads import (
    ABSOLUTE_PATH_CLASS,
    UPLOAD_CLASSES,
    Ending,
    UploadClass,
)

from presentator.adapters.build_wire import (
    LENGTH,
    MAGIC,
    VERSION,
    BuiltOutcome,
    FailedOutcome,
    MessageKind,
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

IMAGE = "presentator"
SOCKET_DIRECTORY = "/run/presentator-build"
SOCKET_PATH = f"{SOCKET_DIRECTORY}/worker.sock"
DECKS_DIRECTORY = "/decks"
HELLO_DECK = f"{DECKS_DIRECTORY}/hello-deck"
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
CELLS = (
    *("5", "6", "48", "49", "51", "52", "54", "A8", "D1", "D2", "D4", "D5", "D6"),
    *("36", "37", "38", "39", "41", "42", "43", "44", "59", "60"),
    *("25", "40", "45", "46", "56", "50"),
)
# The kept-life connections of cell 56 C: each ends in a sweep, none in a restart.
KEPT_LIFE_CONNECTIONS = {
    "answered HELLO": ("hello",),
    "rejected STEP header": ("header-class", "length 0"),
    "H expiry": ("header-class", "dripped past the header deadline"),
}


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


def require_closed_without_a_reply(connection: socket.socket) -> None:
    """Wait for the worker to close the connection having written nothing."""
    try:
        reply = connection.recv(1)
    except ConnectionResetError:
        reply = b""
    except TimeoutError:
        refuse("the connection stayed open")
    if reply:
        refuse("the worker answered a connection it must close")


def expect_closed_without_a_reply(connection: socket.socket) -> None:
    """Require the close, then print that nothing was written."""
    require_closed_without_a_reply(connection)
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


def send_until_closed(connection: socket.socket, data: bytes) -> None:
    """Send the bytes; a close by the worker while sending ends the sending."""
    try:
        connection.sendall(data)
    except (BrokenPipeError, ConnectionResetError):
        return


def drip_until_closed(connection: socket.socket, record: bytes) -> None:
    """Send the record one byte a second; fail if the whole record goes out."""
    for position in range(len(record)):
        try:
            connection.send(record[position : position + 1])
        except (BrokenPipeError, ConnectionResetError):
            return
        time.sleep(1)
    refuse("the worker took a record dripped past its upload time")


def finish_upload(
    connection: socket.socket, row: UploadClass, status_at: float
) -> None:
    """Leave the upload as the row's ending says and print the one line it names."""
    connection.settimeout(CLOSE_SECONDS)
    match row.ending:
        case Ending.REFUSED:
            send_until_closed(connection, row.defect)
            expect_closed_without_a_reply(connection)
        case Ending.DRIPPED:
            drip_until_closed(connection, row.defect)
            require_closed_without_a_reply(connection)
            say(f"closed after {time.monotonic() - status_at:.1f}")
        case Ending.HUNG_UP:
            connection.close()
            say(f"hung up {time.time()!r}")
        case Ending.HALF_CLOSED:
            half_closed_at = time.monotonic()
            connection.shutdown(socket.SHUT_WR)
            require_closed_without_a_reply(connection)
            say(f"closed after {time.monotonic() - half_closed_at:.1f}")


def client_upload_class(name: str) -> None:
    """Send the row's prefix after a STEP's STATUS, then leave as its ending says."""
    row = UPLOAD_CLASSES[name]
    connection = start_build("a")
    status_at = time.monotonic()
    try:
        connection.sendall(row.prefix)
        finish_upload(connection, row, status_at)
    finally:
        connection.close()


def client_hold() -> None:
    """Take a step's STATUS and hold the upload open until the worker is stopped."""
    connection = open_exchange(
        lambda opened: send_step(opened, StepHeader(step=StepKind.BUILD, slug="a"))
    )
    say("status")
    connection.recv(1)
    connection.close()


def start_build(slug: str) -> socket.socket:
    """Open a build STEP of the deck and wait for its STATUS."""
    header = StepHeader(step=StepKind.BUILD, slug=slug)
    return open_exchange(lambda opened: send_step(opened, header))


def upload_deck(connection: socket.socket, slug: str) -> None:
    """Send the deck's tree and END."""
    deck = os.open(Path(DECKS_DIRECTORY) / slug, os.O_RDONLY | os.O_DIRECTORY)
    try:
        send_tree(connection, deck)
    finally:
        os.close(deck)


def client_outcome(slug: str) -> None:
    """Upload the deck and report how the step ended, as a `StepEnd`."""
    connection = start_build(slug)
    try:
        upload_deck(connection, slug)
        if not connection.recv(1, socket.MSG_PEEK):
            say(StepEnd.CLOSED)
            return
        outcome = read_outcome(connection)
    except ConnectionError:
        say(StepEnd.CLOSED)
        return
    except WireRefusedError:
        say(StepEnd.MALFORMED)
        return
    finally:
        connection.close()
    match outcome:
        case FailedOutcome(text=None):
            say(StepEnd.FAILED_WITHOUT_TEXT)
        case FailedOutcome():
            say(StepEnd.FAILED_WITH_TEXT)
        case BuiltOutcome():
            say(StepEnd.BUILT)
        case _:
            say(StepEnd.REFUSED)


def client_abandon(slug: str, how: Abandonment) -> None:
    """Upload the deck, leave after END on the driver's line, print the close time."""
    connection = start_build(slug)
    upload_deck(connection, slug)
    say("uploaded")
    if not sys.stdin.readline():
        refuse("the driver never signalled the abandonment")
    closed_at = time.time()
    if how is Abandonment.HANG_UP:
        connection.close()
        say(f"closed {closed_at!r}")
        return
    connection.shutdown(socket.SHUT_WR)
    say(f"closed {closed_at!r}")
    connection.settimeout(CLOSE_SECONDS * 3)
    expect_closed_without_a_reply(connection)
    connection.close()


def client_step(slug: str, step: StepKind) -> None:
    """Upload the deck, read the outcome and receive the output below /out."""
    header = StepHeader(step=step, slug=slug)
    connection = open_exchange(lambda opened: send_step(opened, header))
    received = OUTPUT_DIRECTORY / f"{step}-{slug}"
    received.mkdir()
    output = os.open(received, os.O_RDONLY | os.O_DIRECTORY)
    try:
        upload_deck(connection, slug)
        outcome = read_outcome(connection)
        if not isinstance(outcome, BuiltOutcome):
            refuse(f"{step} of {slug} answered {outcome}")
        receive_entries(connection, output, OUTPUT_MEGABYTES)
    finally:
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
        case "upload-class", [name]:
            client_upload_class(name)
        case "hold", []:
            client_hold()
        case "step", [slug, step]:
            client_step(slug, StepKind(step))
        case "outcome", [slug]:
            client_outcome(slug)
        case "abandon", [slug, how]:
            client_abandon(slug, Abandonment(how))
        case _:
            sys.exit(__doc__)


# The driver, run where Docker is.


def timed(action: Callable[[], object]) -> float:
    """The seconds the action took."""
    started = time.monotonic()
    action()
    return time.monotonic() - started


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


def prove_wire_user(report: Report) -> None:
    """A8: the wire user is 10002:10002 with no groups and no capabilities."""
    done = subprocess.run(
        ("docker", "exec", "--user", "10002:10002", worker(), "sh", "-c",
         "echo $(id -u):$(id -g):$(id -G); grep -E '^Cap(Eff|Prm)' /proc/self/status"),
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    identity, *capabilities = done.stdout.splitlines() or [""]
    if (
        done.returncode
        or identity != "10002:10002:10002"
        or not capabilities
        or any(line.split()[1].strip("0") for line in capabilities)
    ):
        refuse(f"wire user: {done.returncode} {done.stdout}{done.stderr}")
    report.cell("A8", "exec as 10002:10002: no other group, no capabilities")


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
        f"{PROBE_SOURCES / 'render'}:{DECKS_DIRECTORY}/render:ro",
        "--volume", f"{PROBE_SOURCES / 'attack'}:{DECKS_DIRECTORY}/attack:ro",
        "--volume",
        f"{PROBE_SOURCES / FLOOD_SLUG}:{DECKS_DIRECTORY}/{FLOOD_SLUG}:ro",
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
    if "Probe node alpha" not in words:
        refuse("the exported render PDF carries no mermaid node label")
    report.cell("54", "hello-deck built and exported through the real worker")
    report.cell("D4", "KaTeX markup in the talk; mermaid label in the PDF text")


def prove_fast_lives(report: Report) -> None:
    """Cell 48: a fast life leaves a stale socket that the next life replaces."""
    delays: list[str] = []
    for _ in range(FAST_LIVES):
        _, restarts_before = state()
        probe_client("upload-class", ABSOLUTE_PATH_CLASS)
        delays.append(f"{timed(lambda: probe_client('hello')):.2f}")
        if int(state()[1]) != int(restarts_before) + 1:
            refuse("a fast life did not end in exactly one restart")
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
                 f"{PROBE_DIRECTORY}/{ENTRY_FILE}", "client", "hold"),
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
    report = Report(CELLS)
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
        prove_wire_user(report)
        prove_unsealed_refusal_and_sealed_check(report)
        prove_refused_at_accept(report)
        prove_header_classes(report)
        prove_builds(report)
        prove_attacked_life(report)
        prove_lives_that_end_badly(report, KEPT_LIFE_CONNECTIONS)
        prove_refused_uploads(report)
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
