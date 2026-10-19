"""The wire child's gate judgement and its transitional entry."""

import runpy
import socket
import sys

import pytest

from presentator.host import worker, worker_wire
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
    assert worker.REAL_SERVING.gate_admits(status_of())
    assert not worker.REAL_SERVING.gate_admits(status_of(Groups="0"))


def test_the_exit_status_is_outside_what_a_shell_returns_by_itself() -> None:
    assert WireExit.CLOSED not in {0, 1, 2, 126, 127}
    assert WireExit.CLOSED not in range(128, 256)


def test_the_wire_entry_closes_its_connection_unread() -> None:
    controller_end, child_end = socket.socketpair()
    with controller_end, child_end:
        controller_end.send(b"unread")
        descriptor = child_end.detach()

        status = worker_wire.main(["--connection-fd", str(descriptor)])

        assert status == WireExit.CLOSED
        with pytest.raises(ConnectionResetError):
            controller_end.recv(1)


def test_run_as_a_program_the_entry_exits_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_end, child_end = socket.socketpair()
    with controller_end, child_end:
        descriptor = child_end.detach()
        monkeypatch.setattr(
            sys, "argv", ["worker_wire", "--connection-fd", str(descriptor)]
        )
        monkeypatch.delitem(sys.modules, "presentator.host.worker_wire")

        with pytest.raises(SystemExit) as exited:
            runpy.run_module("presentator.host.worker_wire", run_name="__main__")

        assert exited.value.code == WireExit.CLOSED
        assert controller_end.recv(1) == b""
