"""The build wire's framing: what a HELLO may look like and what STATUS is."""

import socket
from collections.abc import Iterator
from contextlib import closing

import pytest

from presentator.adapters.build_wire import WireRefusedError, read_hello, send_status

SECONDS_TO_WAIT = 5
HELLO = b"PBW\x01\x01\x00\x02{}"


@pytest.fixture
def wire() -> Iterator[tuple[socket.socket, socket.socket]]:
    reading_end, writing_end = socket.socketpair()
    reading_end.settimeout(SECONDS_TO_WAIT)
    writing_end.settimeout(SECONDS_TO_WAIT)
    with closing(reading_end), closing(writing_end):
        yield reading_end, writing_end


def test_status_is_the_wire_format_bytes(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire

    send_status(writing_end)

    assert reading_end.recv(64) == b"PBW\x01\x03\x00\x02{}"


def test_a_hello_with_the_empty_header_is_read(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(HELLO)

    read_hello(reading_end)


@pytest.mark.parametrize("length", [0, 1, 4097, 65535])
def test_a_header_length_outside_two_to_4096_is_refused_before_any_body_read(
    wire: tuple[socket.socket, socket.socket], length: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(b"PBW\x01\x01" + length.to_bytes(2, "big"))

    with pytest.raises(WireRefusedError):
        read_hello(reading_end)


def test_a_header_of_4096_bytes_is_read_to_its_end_and_judged_by_content(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(b"PBW\x01\x01\x10\x00" + b" " * 4094 + b"{}")

    read_hello(reading_end)
