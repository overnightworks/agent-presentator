"""Drive the deployed web against the sealed worker, from the instance image.

Usage: probe_build_worker_web.py drive

Runs where Docker is, after the worker's own drive has removed its project, with
the same compose environment plus `WEB_PROBE_OVERRIDE`, the override file that
adds the web's three read-only probe mounts (the client secret, hello-deck and
`probe_build_worker`). It starts the real `presentator` service, which has no
Docker socket and no neighbour but the worker, and sends the program
`probe_build_worker/web_client.py` into it over stdin: that program builds
through the very composition the web serves with. Every cell prints one line,
and a cell that is not driven fails the run.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from urllib.parse import urlsplit

from probe_build_worker_harness import (
    PROBE_SOURCES,
    REPOSITORY,
    SERVICE,
    Report,
    compose,
    docker,
    refuse,
    say,
    state,
    worker,
)
from probe_build_worker_lives import MODE_FILE, FloodMode, docker_time, wait_for_uptime

from presentator.adapters.build_wire import MAXIMUM_ENTRIES, SOCKET_DIRECTORY
from presentator.adapters.build_worker import (
    CONNECT_WINDOW_SECONDS,
    DECK_PATHS_FOR_A_PAGE,
    DECK_SIZE_FOR_A_PAGE,
    OUTPUT_SHAPE_FOR_A_PAGE,
)
from presentator.adapters.builds import TOO_MANY_FOR_A_PAGE, TOO_MUCH_FOR_A_PAGE
from presentator.host.config import DEFAULT_BUILD_OUTPUT_MEGABYTES

WEB = "presentator"
CLIENT_PROGRAM = PROBE_SOURCES / "web_client.py"
ROUTE = "http://127.0.0.1:8000/login"
BUILDS_ROOT = "/data/builds"
OVERRIDE_MOUNTS = frozenset(
    {"/run/probe-oidc-secret", "/decks/hello-deck", "/decks/probe"}
)
COMPOSE_MOUNTS = frozenset(
    {"/data/database", "/data/mirrors", BUILDS_ROOT, "/run/presentator-speech"}
    | {SOCKET_DIRECTORY}
)
TEMPORARY = "/tmp"  # noqa: S108
ROUTE_POLL_SECONDS = 0.5
STEP_SECONDS = 900
RESTART_SECONDS = 60
CELLS = ("B25", "B12 C", "B13", "B15 C")
UNWRITTEN = "WRITTENNUMBER"
HELLO_STEPS = ("build", "export")


def web_command(*arguments: str) -> tuple[str, ...]:
    """The docker compose command line for the project with the probe override."""
    return (
        "docker", "compose",
        "--file", str(REPOSITORY / "compose.yaml"),
        "--file", os.environ["WEB_PROBE_OVERRIDE"],
        *arguments,
    )  # fmt: skip


def web(*arguments: str) -> str:
    """Run docker compose with the override, failing loudly."""
    return subprocess.run(
        web_command(*arguments), check=True, capture_output=True, text=True
    ).stdout.strip()


def in_web(*command: str) -> subprocess.CompletedProcess[str]:
    """Run a command in the web; the caller judges the status."""
    return subprocess.run(
        web_command("exec", "-T", WEB, *command),
        check=False,
        capture_output=True,
        text=True,
    )


def start_client(case: str, *steps: str) -> "subprocess.Popen[str]":
    """Send the client program into the web; it prints its run's name first."""
    client = subprocess.Popen(
        web_command("exec", "-T", WEB, "/opt/venv/bin/python", "-I", "-", case, *steps),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    if client.stdin is None:
        refuse("the web client has no stdin")
    client.stdin.write(CLIENT_PROGRAM.read_text(encoding="utf-8"))
    client.stdin.close()
    return client


def run_name_of(client: "subprocess.Popen[str]") -> str:
    """The run directory the client builds in, from its first line."""
    if client.stdout is None:
        refuse("the web client has no stdout")
    first = client.stdout.readline()
    if not first:
        refuse(f"the web client ended before it began: {client.communicate()[1]}")
    return str(json.loads(first)["run"])


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """How one step of a client run ended: the page text of a failure, if any."""

    step: str
    failed: bool
    text: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a client run reports: its steps and what its run directory holds."""

    steps: tuple[StepOutcome, ...]
    staged: tuple[str, ...]
    out: tuple[str, ...]
    talk_page: bool
    pdf: bool


def outcome_of(client: "subprocess.Popen[str]") -> Outcome:
    """The client's last line, after it has ended."""
    try:
        stdout, stderr = client.communicate(timeout=STEP_SECONDS)
    except subprocess.TimeoutExpired:
        client.kill()
        refuse(f"the web client did not end within {STEP_SECONDS} seconds")
    lines = stdout.splitlines()
    if client.returncode or not lines:
        refuse(f"the web client failed: {client.returncode} {stderr}")
    said = json.loads(lines[-1])
    return Outcome(
        steps=tuple(
            StepOutcome(step["step"], step["failed"], step.get("text"))
            for step in said["steps"]
        ),
        staged=tuple(said["staged"]),
        out=tuple(said["out"]),
        talk_page=said["talk_page"],
        pdf=said["pdf"],
    )


def build(case: str, *steps: str) -> Outcome:
    """Run the client to its end and return its outcome."""
    client = start_client(case, *steps)
    run_name_of(client)
    return outcome_of(client)


def web_answers() -> bool:
    """Whether the signed-out page answers 200 over the published port."""
    host = urlsplit(os.environ["PRESENTATOR_PUBLIC_ORIGIN"]).hostname or ""
    request = urllib.request.Request(ROUTE, headers={"Host": host})
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310
            return bool(answer.status == HTTPStatus.OK)
    except OSError:
        return False


def wait_until_the_web_answers() -> float:
    """The seconds until the web answers, within the connect window."""
    started = time.monotonic()
    while not web_answers():
        if time.monotonic() - started > CONNECT_WINDOW_SECONDS:
            refuse(f"the web did not answer within {CONNECT_WINDOW_SECONDS:g} seconds")
        time.sleep(ROUTE_POLL_SECONDS)
    return time.monotonic() - started


def restart_count_is(expected: int) -> None:
    """Fail unless the worker restarted exactly to this count, judged by the count."""
    deadline = time.monotonic() + RESTART_SECONDS
    while int(state()[1]) != expected:
        if time.monotonic() > deadline:
            refuse(f"the worker's restart count is {state()[1]}, expected {expected}")
        time.sleep(ROUTE_POLL_SECONDS)


def the_next_build_delivers_hello_deck() -> None:
    """A fresh run of hello-deck builds and exports through the restarted worker."""
    done = build("hello", *HELLO_STEPS)
    if (
        tuple(step.step for step in done.steps) != HELLO_STEPS
        or any(step.failed for step in done.steps)
        or not (done.talk_page and done.pdf)
    ):
        refuse(f"hello-deck was not delivered: {done}")


def prove_inspected_shape() -> None:
    """The web holds no socket, no group, the hardening keys and only ruled mounts."""
    shape = json.loads(docker("inspect", web("ps", "--quiet", WEB)))[0]
    mounts = {
        mount["Destination"]: mount
        for mount in shape["Mounts"]
        if not (mount["Type"] == "tmpfs" and mount["Destination"] == TEMPORARY)
    }
    host = shape["HostConfig"]
    if "docker.sock" in json.dumps(shape["Mounts"]) or host["GroupAdd"]:
        refuse(f"the web holds a socket or a group: {host['GroupAdd']}")
    if (
        host["CapDrop"] != ["ALL"]
        or host["SecurityOpt"] != ["no-new-privileges:true"]
        or host["ReadonlyRootfs"] is not True
    ):
        refuse(f"the web is not hardened: {host}")
    if frozenset(mounts) != COMPOSE_MOUNTS | OVERRIDE_MOUNTS or any(
        mounts[destination]["RW"] for destination in OVERRIDE_MOUNTS
    ):
        refuse(f"the web's mounts are {sorted(mounts)}")


def prove_start_before_the_worker(report: Report) -> None:
    """B25: a web on a fresh socket volume waits, and answers within W of the worker."""
    volume = f"{os.environ['COMPOSE_PROJECT_NAME']}_build-socket"
    if docker("ps", "--all", "--quiet", "--filter", f"volume={volume}"):
        refuse("a container still holds the socket volume")
    web("up", "--detach", "--no-build", "--no-deps", WEB)
    fresh = in_web("stat", "-c", "%a %u %g", SOCKET_DIRECTORY).stdout.strip()
    if fresh != "750 0 0":
        refuse(f"the socket volume is not fresh: {fresh}")
    web("up", "--detach", "--no-build", "--no-deps", SERVICE)
    seconds = wait_until_the_web_answers()
    if "did not answer" in web("logs", "--no-color", WEB):
        refuse("the web's log holds a start refusal")
    prove_inspected_shape()
    scratch = in_web(
        "/opt/venv/bin/python", "-I", "-c", "import tempfile; print(tempfile.mkdtemp())"
    )
    ssh = in_web(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
        "-o", "UserKnownHostsFile=/data/mirrors/known_hosts",
        "-p", "9", "probe@127.0.0.1", "true",
    )  # fmt: skip
    if (
        not scratch.stdout.startswith(TEMPORARY)
        or "Connection refused" not in ssh.stderr
    ):
        refuse(f"the read-only root: {scratch} {ssh}")
    report.cell(
        "B25",
        f"fresh volume 750 0 0; the web answered {seconds:.1f} s after the worker;"
        " no socket, no group, hardened, ruled mounts only; tempfile and ssh start",
    )


def prove_restart_mid_step(report: Report) -> None:
    """B12 C: the web restarts mid-step, starts after the worker's life, builds."""
    wait_for_uptime()
    docker("exec", worker(), "sh", "-c", f"echo {FloodMode.HOLD.value} > {MODE_FILE}")
    _, restarts = state()
    held = start_client("hold", "build")
    run_name_of(held)
    while subprocess.run(
        ("docker", "exec", worker(), "test", "-s", f"{TEMPORARY}/deck.pid"),
        check=False,
    ).returncode:
        if held.poll() is not None:
            refuse("the held step ended before the deck ran")
        time.sleep(ROUTE_POLL_SECONDS)
    web("restart", "--no-deps", WEB)
    held.wait()
    seconds = wait_until_the_web_answers()
    answered = datetime.now(UTC)
    started, _ = state()
    if answered < docker_time(started):
        refuse("the web answered before the worker's new life began")
    restart_count_is(int(restarts) + 1)
    the_next_build_delivers_hello_deck()
    report.cell(
        "B12 C",
        f"restart count {restarts} -> {int(restarts) + 1}; the web answered"
        f" {seconds:.1f} s after its start; hello-deck delivered with %PDF-",
    )


def prove_reply_cut_off(report: Report) -> None:
    """B13: the worker killed mid-reply is a failure, and the next build succeeds."""
    wait_for_uptime()
    _, restarts = state()
    client = start_client("many-entries", "build")
    run = run_name_of(client)
    staged = f"{BUILDS_ROOT}/{run}/staged-build"
    waited = in_web(
        "sh", "-c",
        f'until [ -n "$(ls -A {staged} 2>/dev/null)" ]; do sleep 0.05; done',
    )  # fmt: skip
    if waited.returncode or client.poll() is not None:
        refuse(f"the reply ended before the worker was killed: {waited}")
    pid = docker("inspect", "--format", "{{.State.Pid}}", worker())
    subprocess.run(("sudo", "-n", "kill", "-KILL", pid), check=True)
    done = outcome_of(client)
    if (
        [(step.failed, step.text) for step in done.steps] != [(True, None)]
        or done.staged
        or done.out
    ):
        refuse(f"the cut-off reply: {done}")
    restart_count_is(int(restarts) + 1)
    the_next_build_delivers_hello_deck()
    report.cell(
        "B13",
        f"BuildFailure(text=None); staging gone, out/ empty; restart count"
        f" {restarts} -> {int(restarts) + 1}; the next build delivered",
    )


def text_matching(template: str, **values: object) -> re.Pattern[str]:
    """The pattern of a page text whose `written` is the number the worker reported."""
    filled = template.format(written=UNWRITTEN, **values)
    return re.compile(re.escape(filled).replace(UNWRITTEN, r"(\d+)"))


def failure_text_of(case: str) -> str | None:
    """The text of the one failed build step of a case, whatever it ended in."""
    steps = build(case, "build").steps
    if len(steps) != 1 or not steps[0].failed:
        refuse(f"{case} did not fail: {steps}")
    return steps[0].text


def prove_refusal_texts(report: Report) -> None:
    """B15 C: each limit's page text equals its owner's constant, formatted."""
    allowed = DEFAULT_BUILD_OUTPUT_MEGABYTES
    expected = {
        "output-entries": TOO_MANY_FOR_A_PAGE.format(allowed=MAXIMUM_ENTRIES),
        "output-shape": OUTPUT_SHAPE_FOR_A_PAGE,
    }
    for case, text in expected.items():
        wait_for_uptime()
        if failure_text_of(case) != text:
            refuse(f"{case}: the page text is not {text!r}")
    wait_for_uptime()
    megabytes = text_matching(TOO_MUCH_FOR_A_PAGE, allowed=allowed).fullmatch(
        failure_text_of("output-megabytes") or ""
    )
    if megabytes is None or int(megabytes.group(1)) <= allowed:
        refuse("output-megabytes: the page text does not name a size over the bound")
    restarts, log = state()[1], compose("logs", "--no-color", SERVICE)
    deck_texts = {
        "deck-megabytes": DECK_SIZE_FOR_A_PAGE.format(
            megabytes=allowed, entries=MAXIMUM_ENTRIES
        ),
        "deck-paths": DECK_PATHS_FOR_A_PAGE,
    }
    for case, text in deck_texts.items():
        if failure_text_of(case) != text:
            refuse(f"{case}: the page text is not {text!r}")
    if (state()[1], compose("logs", "--no-color", SERVICE)) != (restarts, log):
        refuse("a deck refused before the connection touched the worker")
    report.cell(
        "B15 C", "five page texts equal their owners'; deck cases left the worker alone"
    )


def drive() -> None:
    """Start the web beside the worker and drive every cell."""
    report = Report(CELLS)
    try:
        prove_start_before_the_worker(report)
        prove_restart_mid_step(report)
        prove_reply_cut_off(report)
        prove_refusal_texts(report)
    finally:
        for service in (WEB, SERVICE):
            say(f"--- {service} log")
            subprocess.run(web_command("logs", "--no-color", service), check=False)
    report.finish()


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["drive"]:
            drive()
        case _:
            sys.exit(__doc__)
