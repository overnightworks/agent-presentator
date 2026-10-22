"""One attacked life and the next: cells 36-39, 41-44, 59 and 60 of the sealed worker.

The attack deck (uid 10001, during `slidev build`) and the exec'd 10002 probe run
in one life, joined by files in the life's `/tmp`, so each attacks the other, the
running step and PID 1 while both are alive. The driver judges what each recorded
in the received talk and on stdout, and then what the next life carries.
"""

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from probe_build_worker_harness import (
    ENTRY_FILE,
    HOLDER,
    OUTPUT_DIRECTORY,
    PROBE_DIRECTORY,
    PROBE_SOURCES,
    SERVICE,
    Report,
    compose,
    extract,
    probe_client,
    refuse,
    say,
    state,
    worker,
)

ATTACK_SLUG = "attack"
ATTACKS_FILE = "attacks.txt"
# The life's shared 1777 tmpfs, by design: the attackers' rendezvous.
TEMPORARY = PurePosixPath("/tmp")  # noqa: S108
PYTHON = "/opt/venv/bin/python"
BUILD_IDENTITY = "10001"
WIRE_IDENTITY = "10002"
WIRE_USER = f"{WIRE_IDENTITY}:{WIRE_IDENTITY}"
DECK_WAIT_SECONDS = 240
PROBE_SECONDS = 150
STEP_SECONDS = 400
NEXT_LIFE_SECONDS = 60
POLL_SECONDS = 0.5
NO_CAPABILITIES = "0000000000000000"
CAPABILITY_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapAmb")
LIFE_ENDED = "life ended:"
SYSV_TABLES = ("shm", "msg", "sem")
MARKER_ROOTS = (str(TEMPORARY), "/dev/shm", "/work")  # noqa: S108
CONFINEMENT_PROFILE = "docker-default (enforce)"
SHARED_CGROUP = "same-as-pid-1"

DECK_REFUSALS = {
    "36": {
        "kill.1.SIGSTOP": "EPERM",
        "kill.1.SIGKILL": "EPERM",
        "kill.probe.SIGSTOP": "EPERM",
        "kill.probe.SIGKILL": "EPERM",
    },
    "37": {"bind.socket-directory": "EACCES", "connect.worker-socket": "EACCES"},
    "38": {
        "ptrace.1": "EPERM",
        "ptrace.probe": "EPERM",
        "read.proc-1-environ": "EACCES",
        "read.proc-1-mem": "EACCES",
    },
    "42": {
        "fs.write.deck": "EACCES",
        "fs.create.root": "EROFS",
        "fs.create.frontend": "EROFS",
        "fs.write.package-json": "EROFS",
    },
    "43": {"net.connect.ip": "ENETUNREACH", "net.dns": "EAI_AGAIN"},
    "44": {"keyctl": "EPERM", "unshare.user": "EPERM"},
}
# The read-only root answers EROFS before the mode is looked at, so the second
# layer (root-owned, not writable by 10001) is observed, not counted as a refusal.
DECK_OBSERVATIONS = {"observe.package-json": "owner=0 mode=0644"}
DECK_CREATIONS = {
    "ipc.shmget": "succeeded",
    "ipc.msgget": "succeeded",
    "ipc.semget": "succeeded",
}
PROBE_REFUSALS = {
    "59": {
        "kill.1.SIGSTOP": "EPERM",
        "kill.1.SIGKILL": "EPERM",
        "kill.step.SIGSTOP": "EPERM",
        "kill.step.SIGKILL": "EPERM",
        "ptrace.1": "EPERM",
        "ptrace.step": "EPERM",
        "read.proc-1-environ": "EACCES",
        "read.proc-1-mem": "EACCES",
        "read.step-mem": "EACCES",
        "bind.socket-directory": "EACCES",
        "connect.worker-socket": "EACCES",
    },
    "60": {
        "create.root": "EROFS",
        "create.opt": "EROFS",
        "create.frontend": "EROFS",
        "create.work": "EACCES",
        "open.step-err": "EACCES",
    },
}


@dataclass(frozen=True)
class Observations:
    """What one attacker recorded: its identity, its checks, what it left behind."""

    statuses: dict[str, dict[str, tuple[str, ...]]] = field(
        default_factory=dict[str, dict[str, tuple[str, ...]]]
    )
    checks: dict[str, str] = field(default_factory=dict[str, str])
    markers: tuple[str, ...] = ()
    children: tuple[tuple[str, str], ...] = ()
    profile: str | None = None
    cgroup: str | None = None
    complete: bool = False
    not_driven: tuple[str, ...] = ()


def parse_observations(text: str) -> Observations:
    """Read the lines an attacker printed; each is `KIND ...`."""
    statuses: dict[str, dict[str, tuple[str, ...]]] = {}
    checks: dict[str, str] = {}
    markers: list[str] = []
    children: list[tuple[str, str]] = []
    not_driven: list[str] = []
    profile: str | None = None
    cgroup: str | None = None
    complete = False
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        match kind, rest.split():
            case "status", [who, name, *values]:
                statuses.setdefault(who, {})[name] = tuple(values)
            case "check" | "observe", [key, *result]:
                checks[key if kind == "check" else f"observe.{key}"] = " ".join(result)
            case "marker", [path]:
                markers.append(path)
            case "child", [pid, uid]:
                children.append((pid, uid))
            case "profile", [_, *_]:
                profile = rest
            case "cgroup", [value]:
                cgroup = value
            case "complete", []:
                complete = True
            case "not-driven", _:
                not_driven.append(rest)
            case _:
                refuse(f"an unknown line from an attacker: {line}")
    return Observations(
        statuses,
        checks,
        tuple(markers),
        tuple(children),
        profile,
        cgroup,
        complete,
        tuple(not_driven),
    )


def identity_mismatches(
    fields: dict[str, tuple[str, ...]], uid: str, groups: tuple[str, ...]
) -> list[str]:
    """The identity facts a count needs that this process's status does not show."""
    expected = {
        "Uid": (uid,) * 4,
        "Gid": (uid,) * 4,
        "Groups": groups,
        "NoNewPrivs": ("1",),
        "Seccomp": ("2",),
        **dict.fromkeys(CAPABILITY_FIELDS, (NO_CAPABILITIES,)),
    }
    return [name for name, value in expected.items() if fields.get(name) != value]


def require_identity(
    observed: Observations, who: str, uid: str, groups: tuple[str, ...] = ()
) -> None:
    """Fail unless the attacker's own status matched before any of its counts."""
    fields = observed.statuses.get(who)
    if fields is None:
        refuse(f"{who} recorded no status line")
    wrong = identity_mismatches(fields, uid, groups)
    if wrong:
        refuse(f"{who} is not {uid}:{uid} bare: {wrong} in {fields}")


def require_confinement(observed: Observations, who: str) -> None:
    """Fail unless the attacker runs under the container's profile and cgroup."""
    if observed.profile != CONFINEMENT_PROFILE:
        refuse(f"{who} runs under {observed.profile!r}, not {CONFINEMENT_PROFILE!r}")
    if observed.cgroup != SHARED_CGROUP:
        refuse(f"{who} cgroup is {observed.cgroup!r}, not PID 1's")


def require_markers(observed: Observations, who: str) -> None:
    """Fail unless this attacker's own writes into every marker root succeeded."""
    failed = {
        key: value
        for key, value in observed.checks.items()
        if key.startswith("marker.")
    }
    if failed:
        refuse(f"{who} could not write its markers: {failed}")
    for root in MARKER_ROOTS:
        if not any(marker.startswith(root) for marker in observed.markers):
            refuse(f"{who} wrote no marker under {root}: {observed.markers}")


def require_checks(observed: Observations, expected: dict[str, str], who: str) -> None:
    """Fail unless every check recorded exactly the errno its row names."""
    if observed.not_driven:
        refuse(f"{who} did not drive: {observed.not_driven}")
    wrong = {
        key: observed.checks.get(key) for key in expected
        if observed.checks.get(key) != expected[key]
    }  # fmt: skip
    if wrong:
        refuse(f"{who} recorded {wrong}, expected {expected}")


def life_endings() -> int:
    """How many lives the worker's log says ended badly."""
    return compose("logs", "--no-color", SERVICE).count(LIFE_ENDED)


def exec_in_worker(*command: str, user: str | None = None) -> list[str]:
    """The `docker exec` argument vector into the worker, program on stdin."""
    who = ("--user", user) if user else ()
    return ["docker", "exec", "-i", *who, worker(), *command]


def start_attack_step() -> "subprocess.Popen[str]":
    """Start the attack deck's build through the holder; the client stays silent."""
    return subprocess.Popen(
        ("docker", "exec", HOLDER, "python", f"{PROBE_DIRECTORY}/{ENTRY_FILE}",
         "client", "step", ATTACK_SLUG, "build"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def wait_for_deck(step: "subprocess.Popen[str]") -> None:
    """Wait until the build code published its pid, so the STEP is accepted."""
    deadline = time.monotonic() + DECK_WAIT_SECONDS
    published = str(TEMPORARY / "deck.pid")
    while subprocess.run(
        exec_in_worker("test", "-s", published), check=False
    ).returncode:
        if step.poll() is not None:
            refuse(f"the attack step ended before the deck ran: {step.communicate()}")
        if time.monotonic() > deadline:
            step.kill()
            refuse("the attack deck's build code never published its pid")
        time.sleep(POLL_SECONDS)


def run_probe() -> str:
    """Run the 10002 probe with its program on stdin and return what it printed."""
    program = (PROBE_SOURCES / "attack" / "kernel_attacks.py").read_text()
    try:
        done = subprocess.run(
            exec_in_worker(PYTHON, "-I", "-", "probe", user=WIRE_USER),
            input=program, capture_output=True, text=True, check=False,
            timeout=PROBE_SECONDS,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        refuse(f"the 10002 probe not driven: no end within {PROBE_SECONDS} seconds")
    if done.returncode:
        refuse(f"the 10002 probe crashed: {done.stdout}{done.stderr}")
    return done.stdout


def finish_step(step: "subprocess.Popen[str]") -> None:
    """The held step answered OUTCOME built and END, having stayed silent."""
    try:
        stdout, stderr = step.communicate(timeout=STEP_SECONDS)
    except subprocess.TimeoutExpired:
        step.kill()
        refuse(f"the attacked step not driven: no end within {STEP_SECONDS} seconds")
    if step.returncode or f"build of {ATTACK_SLUG} built and received" not in stdout:
        refuse(f"the attacked step: {step.returncode} {stdout}{stderr}")


def read_attack_talk() -> Observations:
    """The deck's own lines, from the talk the worker returned."""
    with tempfile.TemporaryDirectory() as received:
        extract(str(OUTPUT_DIRECTORY / f"build-{ATTACK_SLUG}"), Path(received))
        recorded = Path(received, "talk", ATTACKS_FILE)
        if not recorded.is_file():
            refuse(f"the built talk carries no {ATTACKS_FILE}: the hook did not run")
        return parse_observations(recorded.read_text(encoding="utf-8"))


def prove_same_life_answered(restarts_before: int, endings_before: int) -> None:
    """The controller answered in the same life, and the life was consumed."""
    probe_client("hello")
    if int(state()[1]) != restarts_before + 1:
        refuse("the attacked life did not end in exactly one restart")
    if life_endings() != endings_before:
        refuse("the attacked life logged a life ended line")


@dataclass(frozen=True)
class NextLife:
    """What the next life carries of the one before."""

    owners: frozenset[str]
    sysv_objects: int
    markers: tuple[str, ...]


def left_in_next_life(markers: tuple[str, ...]) -> NextLife:
    """Read the next life's processes, SysV tables and the markers' paths."""
    done = subprocess.run(
        exec_in_worker(PYTHON, "-I", "-", *markers),
        input=(PROBE_SOURCES / "next_life.py").read_text(),
        capture_output=True, text=True, check=False, timeout=NEXT_LIFE_SECONDS,
    )  # fmt: skip
    if done.returncode:
        refuse(f"reading the next life: {done.stdout}{done.stderr}")
    left = json.loads(done.stdout)
    return NextLife(
        owners=frozenset(left["owners"]),
        sysv_objects=sum(left["sysv"].values()),
        markers=tuple(left["markers"]),
    )


def prove_nothing_reaches_the_next_life(
    report: Report, deck: Observations, probe: Observations
) -> None:
    """Cells 39 and 41: no 10001 or 10002 process, marker or SysV object survives."""
    expected = (BUILD_IDENTITY, WIRE_IDENTITY)
    if [uid for _, uid in (*deck.children, *probe.children)] != list(expected):
        refuse(f"double-forked children: {deck.children} {probe.children}")
    require_markers(deck, "deck")
    require_markers(probe, "probe")
    markers = (*deck.markers, *probe.markers)
    left = left_in_next_life(markers)
    if left.owners & set(expected):
        refuse(f"the next life carries processes of {sorted(left.owners)}")
    if left.markers or left.sysv_objects:
        refuse(f"the next life carries {left}")
    report.cell("39", "both double-forked children gone; no 10001 or 10002 process")
    report.cell(
        "41", f"{len(markers)} markers and 3 SysV objects absent in the next life"
    )


def prove_attacked_life(report: Report) -> None:
    """Cells 36-39, 41-44, 59 and 60: one attacked life and the next."""
    probe_client("hello")
    restarts_before = int(state()[1])
    endings_before = life_endings()
    step = start_attack_step()
    wait_for_deck(step)
    probe = parse_observations(run_probe())
    finish_step(step)
    deck = read_attack_talk()
    # `docker exec --user 10002:10002` lists the primary group, no other.
    require_identity(probe, "probe", WIRE_IDENTITY, (WIRE_IDENTITY,))
    require_confinement(probe, "probe")
    if not probe.complete:
        refuse(f"the probe did not complete: {probe}")
    for who in ("node", "python"):
        require_identity(deck, who, BUILD_IDENTITY)
    require_checks(deck, DECK_CREATIONS, "deck")
    require_checks(deck, DECK_OBSERVATIONS, "deck observations")
    for cell, expected in DECK_REFUSALS.items():
        require_checks(deck, expected, f"deck cell {cell}")
    for cell, expected in PROBE_REFUSALS.items():
        require_checks(probe, expected, f"probe cell {cell}")
    prove_same_life_answered(restarts_before, endings_before)
    for cell, recorded in {**DECK_REFUSALS, **PROBE_REFUSALS}.items():
        report.cell(
            cell, ", ".join(f"{key} {errno}" for key, errno in recorded.items())
        )
    for name in DECK_OBSERVATIONS:
        say(f"{name} {deck.checks[name]}")
    prove_nothing_reaches_the_next_life(report, deck, probe)
    say("attacked life judged")
