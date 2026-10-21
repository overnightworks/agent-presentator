"""The wire child's gate judgement and its idle entry."""

import errno
import os
import runpy
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from presentator.adapters.build_wire import MEGABYTE, StepHeader, write_record
from presentator.contracts.decks import StepKind
from presentator.host import worker, worker_wire
from presentator.host.config import WorkerSettings
from presentator.host.worker import create_request_record
from presentator.host.worker_wire import WireExit, WireRole
from tests.worker_support import (
    END,
    HELLO,
    OUTCOME_FAILED,
    STATUS,
    STEP_PREAMBLE,
    UNUSED_DECK,
    end_entry,
    file_entry,
    file_header,
    framed,
)

WIRE_ID = worker.WIRE_IDENTITY[0]
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
    assert worker.wire_gate_admits(status_of(), worker.WIRE_IDENTITY)


@pytest.mark.parametrize("changes", BROKEN_ONE_WAY.values(), ids=BROKEN_ONE_WAY.keys())
def test_the_gate_refuses_a_status_broken_in_one_way(
    changes: dict[str, str | None],
) -> None:
    assert not worker.wire_gate_admits(status_of(**changes), worker.WIRE_IDENTITY)


def test_the_gate_judges_against_the_identity_it_is_given() -> None:
    assert not worker.wire_gate_admits(status_of(), (WIRE_ID + 1, WIRE_ID))
    assert not worker.wire_gate_admits(status_of(), (WIRE_ID, WIRE_ID + 1))


def test_production_judges_by_the_gate_of_the_wire_identity() -> None:
    serving = worker.real_serving(WorkerSettings(build_timeout_seconds=9.0))

    assert serving.gate_admits(status_of())
    assert not serving.gate_admits(status_of(Groups="0"))


def test_production_carries_the_build_bound_of_the_settings() -> None:
    settings = WorkerSettings(build_timeout_seconds=9.0, build_output_megabytes=7)

    serving = worker.real_serving(settings)

    assert (serving.build_timeout_seconds, serving.build_output_megabytes) == (9.0, 7)


def test_production_hands_the_deck_effects_the_deck_it_carries() -> None:
    serving = worker.real_serving(WorkerSettings())

    assert serving.open_deck is worker.open_deck
    assert serving.close_deck is worker.close_deck
    assert serving.deck == Path(worker.WORK_MOUNT) / "deck"


def test_the_exit_status_is_outside_what_a_shell_returns_by_itself() -> None:
    for status in WireExit:
        assert status not in {0, 1, 2, 126, 127}
        assert status not in range(128, 256)


SECONDS_TO_WAIT = 5
BOM = b"\xef\xbb\xbf"


EXPORT_STEP = framed(b'{"step":"export","slug":"my-deck"}', STEP_PREAMBLE)


REFUSED_INPUTS = {
    "wrong magic X": framed(b"{}", b"XBW\x01\x01"),
    "wrong magic last byte": framed(b"{}", b"PBX\x01\x01"),
    "version 0": framed(b"{}", b"PBW\x00\x01"),
    "version 2": framed(b"{}", b"PBW\x02\x01"),
    "kind 0": framed(b"{}", b"PBW\x01\x00"),
    "kind BUSY": framed(b"{}", b"PBW\x01\x04"),
    "kind OUTCOME": framed(b"{}", b"PBW\x01\x05"),
    "kind STATUS": framed(b"{}", b"PBW\x01\x03"),
    "kind 6": framed(b"{}", b"PBW\x01\x06"),
    "kind 255": framed(b"{}", b"PBW\x01\xff"),
    "a STEP header with no step": framed(b"{}", STEP_PREAMBLE),
    "a STEP header with an extra key": framed(
        b'{"step":"build","slug":"a","more":1}', STEP_PREAMBLE
    ),
    "a STEP header with an unknown step": framed(
        b'{"step":"x","slug":"a"}', STEP_PREAMBLE
    ),
    "a STEP header with a slug leaving its folder": framed(
        b'{"step":"build","slug":"../a"}', STEP_PREAMBLE
    ),
    "a STEP header with a null slug": framed(
        b'{"step":"build","slug":null}', STEP_PREAMBLE
    ),
    "a STEP header not UTF-8": framed(b"\xff\xfe", STEP_PREAMBLE),
    "a STEP header length 4097": STEP_PREAMBLE + b"\x10\x01" + b" " * 4097,
    "nothing": b"",
    "EOF inside the preamble": b"PB",
    "header length 0": b"PBW\x01\x01\x00\x00",
    "header length 1": b"PBW\x01\x01\x00\x01{",
    "header length 4097": b"PBW\x01\x01\x10\x01" + b" " * 4097,
    "EOF inside the length": b"PBW\x01\x01\x00",
    "header not UTF-8": framed(b"\xff\xfe"),
    "header a list": framed(b"[]"),
    "header null": framed(b"null"),
    "header with a key": framed(b'{"a":1}'),
    "header two objects": framed(b"{}{}"),
    "header with a BOM": framed(BOM + b"{}"),
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
    deck: Path = UNUSED_DECK,
    megabytes: int = 300,
) -> list[str]:
    return [
        "--role",
        role,
        "--connection-fd",
        str(descriptor),
        "--record-fd",
        str(record),
        "--deck",
        str(deck),
        "--megabytes",
        str(megabytes),
    ]


def run_entry(
    client: socket.socket,
    descriptor: int,
    record: int,
    role: WireRole = WireRole.IDLE,
    deck: Path = UNUSED_DECK,
) -> object:
    return run_bounded(
        client,
        lambda: worker_wire.main(entry_arguments(descriptor, record, role, deck)),
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
    client.sendall(HELLO + b"x")

    assert run_entry(client, descriptor, record) == WireExit.ANSWERED
    assert client.recv(64) == STATUS
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


def test_the_upload_entry_answers_status_then_receives_into_the_deck_exiting_uploaded(
    client_and_descriptor: tuple[socket.socket, int], record: int, tmp_path: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.sendall(file_entry(b"slide.md", b"# hi") + end_entry(1, 4))

    assert (
        run_entry(client, descriptor, record, WireRole.UPLOAD, tmp_path)
        == WireExit.UPLOADED
    )
    assert client.recv(64) == STATUS
    assert (tmp_path / "slide.md").read_bytes() == b"# hi"


def test_the_upload_entry_enforces_the_megabytes_of_its_argument(
    client_and_descriptor: tuple[socket.socket, int], record: int, tmp_path: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.sendall(file_header(b"f", MEGABYTE + 1))

    assert (
        run_bounded(
            client,
            lambda: worker_wire.main(
                entry_arguments(descriptor, record, WireRole.UPLOAD, tmp_path, 1)
            ),
        )
        == WireExit.CLOSED
    )
    assert client.recv(64) == STATUS
    assert not (tmp_path / "f").exists()


def test_the_upload_entry_accepts_what_its_larger_argument_allows(
    client_and_descriptor: tuple[socket.socket, int], record: int, tmp_path: Path
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
                entry_arguments(descriptor, record, WireRole.UPLOAD, tmp_path, 2)
            ),
        )
    finally:
        sender.join(SECONDS_TO_WAIT)

    assert not sender.is_alive()
    assert exit_status == WireExit.UPLOADED
    assert (tmp_path / "f").read_bytes() == content


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
    client_and_descriptor: tuple[socket.socket, int], record: int, tmp_path: Path
) -> None:
    client, descriptor = client_and_descriptor
    write_record(record, StepHeader(step=StepKind.EXPORT, slug="my-deck"))
    client.shutdown(socket.SHUT_WR)

    assert (
        run_entry(client, descriptor, record, WireRole.UPLOAD, tmp_path)
        == WireExit.CLOSED
    )
    assert client.recv(64) == STATUS


def test_the_send_entry_writes_outcome_failed_and_end_exiting_sent(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor

    assert run_entry(client, descriptor, record, WireRole.SEND) == WireExit.SENT
    assert client.recv(64) == OUTCOME_FAILED + END


def test_the_send_entry_exits_closed_when_the_peer_has_hung_up(
    client_and_descriptor: tuple[socket.socket, int], record: int
) -> None:
    client, descriptor = client_and_descriptor
    client.close()

    assert run_entry(client, descriptor, record, WireRole.SEND) == WireExit.CLOSED


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
    client.sendall(HELLO)
    client.close()

    assert run_entry(client, descriptor, record) == WireExit.CLOSED


def test_run_as_a_program_the_entry_answers_a_hello(
    monkeypatch: pytest.MonkeyPatch, record: int
) -> None:
    controller_end, child_end = socket.socketpair()
    controller_end.settimeout(SECONDS_TO_WAIT)
    with controller_end, child_end:
        controller_end.sendall(HELLO)
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
        assert controller_end.recv(64) == STATUS
