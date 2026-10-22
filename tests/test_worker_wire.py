"""The wire child's gate judgement and its idle entry."""

import errno
import json
import os
import runpy
import socket
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from presentator.adapters.build_wire import (
    LENGTH,
    MAGIC,
    MEGABYTE,
    StepHeader,
    write_record,
)
from presentator.adapters.worker_children import ChildIdentity, ChildSurroundings
from presentator.contracts.decks import StepKind
from presentator.host import worker, worker_wire
from presentator.host.config import WorkerSettings
from presentator.host.worker import create_request_record
from presentator.host.worker_wire import ReplyPlan, WireExit, WireRole
from tests import worker_support as support
from tests.worker_support import (
    UNUSED_WORK,
    end_entry,
    file_entry,
    file_header,
)

WIRE_ID = worker.WIRE_IDENTITY[0]
BUILD_ID = worker.BUILD_IDENTITY[0]
GATED_FIELDS = {
    "Name": "sh",
    "Uid": "\t".join([str(WIRE_ID)] * 4),
    "Gid": "\t".join([str(WIRE_ID)] * 4),
    "Groups": "",
    "CapInh": "0000000000000000",
    "CapPrm": "0000000000000000",
    "CapEff": "0000000000000000",
    "CapBnd": "0000000000000000",
    "CapAmb": "0000000000000000",
    "NoNewPrivs": "1",
    "Seccomp": "2",
}


def status_of(**changes: str | None) -> str:
    fields = {**GATED_FIELDS, **changes}
    return "\n".join(
        f"{name}:\t{value}" for name, value in fields.items() if value is not None
    )


def uid_with(position: int, value: str) -> str:
    ids = [str(WIRE_ID)] * 4
    ids[position] = value
    return "\t".join(ids)


BROKEN_ONE_WAY = {
    **{
        f"uid position {position}": {"Uid": uid_with(position, "0")}
        for position in range(4)
    },
    **{
        f"gid position {position}": {"Gid": uid_with(position, "0")}
        for position in range(4)
    },
    "another user's uid": {"Uid": "\t".join(["10001"] * 4)},
    "a supplementary group": {"Groups": "10002"},
    "inheritable capability": {"CapInh": "0000000000000001"},
    "permitted capability": {"CapPrm": "0000000000000001"},
    "effective capability": {"CapEff": "0000000000000001"},
    "ambient capability": {"CapAmb": "0000000000000001"},
    "privileges may grow": {"NoNewPrivs": "0"},
    "no seccomp filter": {"Seccomp": "0"},
    "seccomp strict mode": {"Seccomp": "1"},
    **{
        f"missing {name}": {name: None}
        for name in (
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
    },
}


def test_a_sealed_child_of_the_wire_user_passes_the_gate() -> None:
    assert worker.child_gate_admits(status_of(), worker.WIRE_IDENTITY)


@pytest.mark.parametrize("changes", BROKEN_ONE_WAY.values(), ids=BROKEN_ONE_WAY.keys())
def test_the_gate_refuses_a_status_broken_in_one_way(
    changes: dict[str, str | None],
) -> None:
    assert not worker.child_gate_admits(status_of(**changes), worker.WIRE_IDENTITY)


def test_the_gate_judges_against_the_identity_it_is_given() -> None:
    assert not worker.child_gate_admits(status_of(), (WIRE_ID + 1, WIRE_ID))
    assert not worker.child_gate_admits(status_of(), (WIRE_ID, WIRE_ID + 1))


def test_production_judges_by_the_gate_of_the_wire_identity() -> None:
    serving = worker.real_serving(WorkerSettings(build_timeout_seconds=9.0))

    assert serving.gate_admits(status_of())
    assert not serving.gate_admits(status_of(Groups="0"))


def test_production_runs_the_step_as_the_build_user_behind_a_build_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[dict[str, object]] = []

    def record_spawn(_command: Sequence[str], **keywords: object) -> None:
        spawned.append(keywords)

    monkeypatch.setattr(worker, "spawn_gated", record_spawn)
    serving = worker.real_serving(WorkerSettings())
    build_ids = "\t".join([str(BUILD_ID)] * 4)

    serving.step.spawn(
        ["launcher"], environment={}, pass_fds=(), surroundings=ChildSurroundings()
    )

    assert spawned[0]["identity"] == ChildIdentity(BUILD_ID, BUILD_ID, extra_groups=())
    assert serving.step.gate_admits(status_of(Uid=build_ids, Gid=build_ids))
    assert not serving.step.gate_admits(status_of())
    assert not serving.step.gate_admits(
        status_of(Uid=build_ids, Gid=build_ids, Groups="0")
    )


def test_production_gives_the_step_exactly_six_environment_keys_and_the_project() -> (
    None
):
    serving = worker.real_serving(WorkerSettings())

    assert serving.step.environment == {
        "PATH": os.environ["PATH"],
        "HOME": "/work/home",
        "TMPDIR": str(Path("/") / "tmp"),
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright",
        "COREPACK_HOME": "/opt/corepack",
        "COREPACK_ENABLE_NETWORK": "0",
    }
    assert serving.step.project == Path("/app/frontend")


def test_production_carries_the_build_bound_of_the_settings() -> None:
    settings = WorkerSettings(build_timeout_seconds=9.0, build_output_megabytes=7)

    serving = worker.real_serving(settings)

    assert (serving.build_timeout_seconds, serving.build_output_megabytes) == (9.0, 7)


def test_production_hands_the_deck_effects_the_deck_it_carries() -> None:
    serving = worker.real_serving(WorkerSettings())

    assert serving.open_deck is worker.make_open_directory
    assert serving.close_deck is worker.close_deck
    assert serving.work == Path(worker.WORK_MOUNT)


def test_the_exit_status_is_outside_what_a_shell_returns_by_itself() -> None:
    for status in WireExit:
        assert status not in {0, 1, 2, 126, 127}
        assert status not in range(128, 256)


SECONDS_TO_WAIT = 5
BOM = b"\xef\xbb\xbf"


EXPORT_STEP = support.framed(b'{"step":"export","slug":"my-deck"}', support.STEP)


REFUSED_INPUTS = {
    "wrong magic X": support.framed(b"{}", b"XBW\x01\x01"),
    "wrong magic last byte": support.framed(b"{}", b"PBX\x01\x01"),
    "version 0": support.framed(b"{}", b"PBW\x00\x01"),
    "version 2": support.framed(b"{}", b"PBW\x02\x01"),
    "kind 0": support.framed(b"{}", b"PBW\x01\x00"),
    "kind BUSY": support.framed(b"{}", b"PBW\x01\x04"),
    "kind OUTCOME": support.framed(b"{}", b"PBW\x01\x05"),
    "kind STATUS": support.framed(b"{}", b"PBW\x01\x03"),
    "kind 6": support.framed(b"{}", b"PBW\x01\x06"),
    "kind 255": support.framed(b"{}", b"PBW\x01\xff"),
    "a STEP header with no step": support.framed(b"{}", support.STEP),
    "a STEP header with an extra key": support.framed(
        b'{"step":"build","slug":"a","more":1}', support.STEP
    ),
    "a STEP header with an unknown step": support.framed(
        b'{"step":"x","slug":"a"}', support.STEP
    ),
    "a STEP header with a slug leaving its folder": support.framed(
        b'{"step":"build","slug":"../a"}', support.STEP
    ),
    "a STEP header with a null slug": support.framed(
        b'{"step":"build","slug":null}', support.STEP
    ),
    "a STEP header not UTF-8": support.framed(b"\xff\xfe", support.STEP),
    "a STEP header length 4097": support.STEP + b"\x10\x01" + b" " * 4097,
    "nothing": b"",
    "EOF inside the preamble": b"PB",
    "header length 0": b"PBW\x01\x01\x00\x00",
    "header length 1": b"PBW\x01\x01\x00\x01{",
    "header length 4097": b"PBW\x01\x01\x10\x01" + b" " * 4097,
    "EOF inside the length": b"PBW\x01\x01\x00",
    "header not UTF-8": support.framed(b"\xff\xfe"),
    "header a list": support.framed(b"[]"),
    "header null": support.framed(b"null"),
    "header with a key": support.framed(b'{"a":1}'),
    "header two objects": support.framed(b"{}{}"),
    "header with a BOM": support.framed(BOM + b"{}"),
    "EOF inside the body": b"PBW\x01\x01\x00\x05{}",
}


@pytest.fixture
def client_and_descriptor() -> Iterator[tuple[socket.socket, int]]:
    client, child_end = socket.socketpair()
    client.settimeout(SECONDS_TO_WAIT)
    with closing(client):
        yield client, child_end.detach()


@pytest.fixture
def record() -> Iterator[int]:
    descriptor = create_request_record()
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def run_bounded(client: socket.socket, entry: Callable[[], object]) -> object:
    """Fail, after freeing the entry, when it outlasts the wait."""
    outcome: list[object] = []

    def target() -> None:
        try:
            outcome.append(entry())
        except SystemExit as exited:
            outcome.append(exited.code)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(SECONDS_TO_WAIT)
    if thread.is_alive():
        client.close()
        thread.join(SECONDS_TO_WAIT)
        pytest.fail("the entry waited for more input than a HELLO")
    return outcome[0]


def entry_arguments(
    descriptor: int,
    record: int,
    role: WireRole,
    work: Path = UNUSED_WORK,
    megabytes: int = 300,
) -> list[str]:
    return [
        "--role",
        role,
        "--connection-fd",
        str(descriptor),
        "--record-fd",
        str(record),
        "--work",
        str(work),
        "--megabytes",
        str(megabytes),
    ]


def run_entry(
    client: socket.socket,
    descriptor: int,
    record: int,
    role: WireRole = WireRole.IDLE,
    work: Path = UNUSED_WORK,
) -> object:
    return run_bounded(
        client,
        lambda: worker_wire.main(entry_arguments(descriptor, record, role, work)),
    )


def run_sender(
    client: socket.socket,
    descriptor: int,
    record: int,
    work: Path = UNUSED_WORK,
    plan: ReplyPlan | None = None,
) -> object:
    plan_arguments = ["--plan", plan] if plan is not None else []
    return run_bounded(
        client,
        lambda: worker_wire.main(
            [
                *entry_arguments(descriptor, record, WireRole.SEND, work),
                *plan_arguments,
            ]
        ),
    )


def read_to_end_or_reset(client: socket.socket) -> bytes:
    received = b""
    try:
        while chunk := client.recv(64):
            received += chunk
    except ConnectionResetError:
        pass
    return received


@pytest.mark.parametrize("sent", REFUSED_INPUTS.values(), ids=REFUSED_INPUTS.keys())
def test_the_entry_closes_a_refused_input_with_no_reply(
    client_and_descriptor: tuple[socket.socket, int], record: int, sent: bytes
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(sent)
    client.shutdown(socket.SHUT_WR)

    assert run_entry(client, descriptor, record) == WireExit.CLOSED
    assert read_to_end_or_reset(client) == b""
    assert os.fstat(record).st_size == 0


def test_the_entry_answers_a_hello_with_status_and_exits_answered(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(support.HELLO + b"x")

    assert run_entry(client, descriptor, record) == WireExit.ANSWERED
    assert client.recv(64) == support.STATUS
    assert os.fstat(record).st_size == 0


def test_the_entry_exits_accepted_on_a_valid_step_writing_the_record_and_no_reply(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(EXPORT_STEP)

    assert run_entry(client, descriptor, record) == WireExit.ACCEPTED
    assert client.recv(64) == b""
    assert os.pread(record, 4096, 0) == b'{"step":"export","slug":"my-deck"}'
    assert os.lseek(record, 0, os.SEEK_CUR) == 0


@pytest.fixture
def work(tmp_path: Path) -> Path:
    (tmp_path / "deck").mkdir()
    return tmp_path


def test_the_upload_entry_answers_status_then_receives_into_the_deck_exiting_uploaded(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.sendall(file_entry(b"slide.md", b"# hi") + end_entry(1, 4))

    assert (
        run_entry(client, descriptor, record, WireRole.UPLOAD, work)
        == WireExit.UPLOADED
    )
    assert client.recv(64) == support.STATUS
    assert (work / "deck" / "slide.md").read_bytes() == b"# hi"


def test_the_upload_entry_enforces_the_megabytes_of_its_argument(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.sendall(file_header(b"f", MEGABYTE + 1))

    assert (
        run_bounded(
            client,
            lambda: worker_wire.main(
                entry_arguments(descriptor, record, WireRole.UPLOAD, work, 1)
            ),
        )
        == WireExit.CLOSED
    )
    assert client.recv(64) == support.STATUS
    assert not (work / "deck" / "f").exists()


def test_the_upload_entry_accepts_what_its_larger_argument_allows(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    content = os.urandom(MEGABYTE + 1)
    sender = threading.Thread(
        target=client.sendall,
        args=(file_entry(b"f", content) + end_entry(1, len(content)),),
    )
    sender.start()
    try:
        exit_status = run_bounded(
            client,
            lambda: worker_wire.main(
                entry_arguments(descriptor, record, WireRole.UPLOAD, work, 2)
            ),
        )
    finally:
        sender.join(SECONDS_TO_WAIT)

    assert not sender.is_alive()
    assert exit_status == WireExit.UPLOADED
    assert (work / "deck" / "f").read_bytes() == content


def test_the_upload_entry_exits_closed_before_status_when_the_deck_cannot_be_opened(
    client_and_descriptor: tuple[socket.socket, int], record: int, tmp_path: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.sendall(end_entry(0, 0))

    assert (
        run_entry(client, descriptor, record, WireRole.UPLOAD, tmp_path / "missing")
        == WireExit.CLOSED
    )
    assert read_to_end_or_reset(client) == b""


def test_the_upload_entry_exits_closed_when_the_peer_hangs_up_before_end(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.shutdown(socket.SHUT_WR)

    assert (
        run_entry(client, descriptor, record, WireRole.UPLOAD, work) == WireExit.CLOSED
    )
    assert client.recv(64) == support.STATUS


def sent_text(received: bytes) -> str | None:
    """The text of the OUTCOME in a reply that ends with END and nothing else."""
    assert received.endswith(support.END)
    header = received[len(MAGIC) + 2 + LENGTH.size :]
    return json.loads(header[: -len(support.END)])["text"]


@pytest.mark.parametrize(
    "case",
    [
        (ReplyPlan.FAILED_NULL, b"ignored", None),
        (ReplyPlan.FAILED_TEXT, b"x" * 3000, "x" * 2000),
        (
            ReplyPlan.FAILED_TEXT,
            b"a" * 1_048_576 + b"b" * 1999 + b"c",
            "b" * 1999 + "c",
        ),
        (ReplyPlan.FAILED_TEXT, b" \n ", None),
        (ReplyPlan.FAILED_TEXT, b"bad \xff\xfe bytes", "bad \ufffd\ufffd bytes"),
    ],
    ids=["null", "ascii flood", "megabyte flood", "blank", "invalid utf-8"],
)
def test_the_send_entry_answers_failed_with_the_planned_text_exiting_sent(
    client_and_descriptor: tuple[socket.socket, int],
    record: int,
    work: Path,
    case: tuple[ReplyPlan, bytes, str | None],
) -> None:
    plan, step_error, expected_text = case
    client, descriptor = client_and_descriptor
    (work / worker_wire.STEP_ERROR_FILE).write_bytes(step_error)

    assert run_sender(client, descriptor, record, work, plan) == WireExit.SENT
    assert sent_text(read_to_end_or_reset(client)) == expected_text


def test_the_send_entry_under_walk_refuses_an_empty_output_as_shape_exiting_sent(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    """Cell W2 (30)."""
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.BUILD, slug="my-deck"))
    (work / "out").mkdir()

    assert run_sender(client, descriptor, record, work, ReplyPlan.WALK) == (
        WireExit.SENT
    )
    assert read_to_end_or_reset(client) == support.OUTCOME_REFUSED_SHAPE + support.END


def test_the_send_entry_under_walk_exits_closed_without_a_reply_when_out_is_absent(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    """Decision 1: any other OSError passes through, so the life ends without a reply."""
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.BUILD, slug="my-deck"))

    assert run_sender(client, descriptor, record, work, ReplyPlan.WALK) == (
        WireExit.CLOSED
    )
    assert read_to_end_or_reset(client) == b""


def test_the_send_entry_sends_nothing_without_a_plan(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor

    assert run_sender(client, descriptor, record) == WireExit.CLOSED
    assert read_to_end_or_reset(client) == b""


def test_the_send_entry_exits_closed_when_the_text_file_cannot_be_read(
    client_and_descriptor: tuple[socket.socket, int], record: int, work: Path
) -> None:
    client, descriptor = client_and_descriptor

    exit_status = run_sender(client, descriptor, record, work, ReplyPlan.FAILED_TEXT)

    assert exit_status == WireExit.CLOSED
    assert read_to_end_or_reset(client) == b""


def test_the_send_entry_exits_closed_when_the_peer_has_hung_up(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor
    client.close()

    exit_status = run_sender(client, descriptor, record, plan=ReplyPlan.FAILED_NULL)

    assert exit_status == WireExit.CLOSED


def test_a_refused_memfd_surfaces_as_the_kernels_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def memfd_failure(*_call: object) -> int:
        return -1

    def refusing_libc(*_arguments: object, **_keywords: object) -> SimpleNamespace:
        return SimpleNamespace(memfd_create=memfd_failure)

    monkeypatch.setattr(worker.ctypes, "CDLL", refusing_libc)
    monkeypatch.setattr(worker.ctypes, "get_errno", lambda: errno.EMFILE)

    with pytest.raises(OSError, match="memfd_create failed") as refused:
        create_request_record()

    assert refused.value.errno == errno.EMFILE


def test_the_entry_closes_when_the_client_hung_up_before_status(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(support.HELLO)
    client.close()

    assert run_entry(client, descriptor, record) == WireExit.CLOSED


def test_run_as_a_program_the_entry_answers_a_hello(
    monkeypatch: pytest.MonkeyPatch, record: int
) -> None:
    controller_end, child_end = socket.socketpair()
    controller_end.settimeout(SECONDS_TO_WAIT)
    with controller_end, child_end:
        controller_end.sendall(support.HELLO)
        descriptor = child_end.detach()
        monkeypatch.setattr(
            sys,
            "argv",
            ["worker_wire", *entry_arguments(descriptor, record, WireRole.IDLE)],
        )
        monkeypatch.delitem(sys.modules, "presentator.host.worker_wire")

        exit_code = run_bounded(
            controller_end,
            lambda: runpy.run_module(
                "presentator.host.worker_wire", run_name="__main__"
            ),
        )

        assert exit_code == WireExit.ANSWERED
        assert controller_end.recv(64) == support.STATUS
