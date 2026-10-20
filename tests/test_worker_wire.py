"""The wire child's gate judgement and its idle entry."""

import runpy
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import closing

import pytest

from presentator.host import worker, worker_wire
from presentator.host.config import WorkerSettings
from presentator.host.worker_wire import WireExit

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
    settings = WorkerSettings(build_timeout_seconds=9.0)

    serving = worker.real_serving(settings)

    assert serving.build_timeout_seconds == settings.build_timeout_seconds


def test_the_exit_status_is_outside_what_a_shell_returns_by_itself() -> None:
    for status in WireExit:
        assert status not in {0, 1, 2, 126, 127}
        assert status not in range(128, 256)


HELLO = b"PBW\x01\x01\x00\x02{}"
SECONDS_TO_WAIT = 5
BOM = b"\xef\xbb\xbf"
STEP = b"PBW\x01\x02"


def framed(header: bytes, preamble: bytes = b"PBW\x01\x01") -> bytes:
    return preamble + len(header).to_bytes(2, "big") + header


VALID_STEP = framed(b'{"step":"export","slug":"my-deck"}', STEP)


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
    "a STEP header with no step": framed(b"{}", STEP),
    "a STEP header with an extra key": framed(
        b'{"step":"build","slug":"a","more":1}', STEP
    ),
    "a STEP header with an unknown step": framed(b'{"step":"x","slug":"a"}', STEP),
    "a STEP header with a slug leaving its folder": framed(
        b'{"step":"build","slug":"../a"}', STEP
    ),
    "a STEP header with a null slug": framed(b'{"step":"build","slug":null}', STEP),
    "a STEP header not UTF-8": framed(b"\xff\xfe", STEP),
    "a STEP header length 4097": STEP + b"\x10\x01" + b" " * 4097,
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


def run_entry(client: socket.socket, descriptor: int) -> object:
    return run_bounded(
        client, lambda: worker_wire.main(["--connection-fd", str(descriptor)])
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
    client_and_descriptor: tuple[socket.socket, int], sent: bytes
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(sent)
    client.shutdown(socket.SHUT_WR)

    assert run_entry(client, descriptor) == WireExit.CLOSED
    assert read_to_end_or_reset(client) == b""


def test_the_entry_answers_a_hello_with_status_and_exits_answered(
    client_and_descriptor: tuple[socket.socket, int],
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(HELLO + b"x")

    assert run_entry(client, descriptor) == WireExit.ANSWERED
    assert client.recv(64) == b"PBW\x01\x03\x00\x02{}"


def test_the_entry_exits_accepted_on_a_valid_step_and_replies_nothing(
    client_and_descriptor: tuple[socket.socket, int],
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(VALID_STEP)

    assert run_entry(client, descriptor) == WireExit.ACCEPTED
    assert client.recv(64) == b""


def test_the_entry_closes_when_the_client_hung_up_before_status(
    client_and_descriptor: tuple[socket.socket, int],
) -> None:
    client, descriptor = client_and_descriptor
    client.sendall(HELLO)
    client.close()

    assert run_entry(client, descriptor) == WireExit.CLOSED


def test_run_as_a_program_the_entry_answers_a_hello(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_end, child_end = socket.socketpair()
    controller_end.settimeout(SECONDS_TO_WAIT)
    with controller_end, child_end:
        controller_end.sendall(HELLO)
        descriptor = child_end.detach()
        monkeypatch.setattr(
            sys, "argv", ["worker_wire", "--connection-fd", str(descriptor)]
        )
        monkeypatch.delitem(sys.modules, "presentator.host.worker_wire")

        exit_code = run_bounded(
            controller_end,
            lambda: runpy.run_module(
                "presentator.host.worker_wire", run_name="__main__"
            ),
        )

        assert exit_code == WireExit.ANSWERED
        assert controller_end.recv(64) == b"PBW\x01\x03\x00\x02{}"
