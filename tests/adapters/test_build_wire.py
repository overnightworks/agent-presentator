"""The build wire's framing: what a HELLO or STEP may look like and what STATUS is."""

import socket
from collections.abc import Iterator
from contextlib import closing

import pytest

from presentator.adapters.build_wire import (
    EmptyHeader,
    StepHeader,
    WireRefusedError,
    read_request,
    send_status,
)
from presentator.contracts.decks import StepKind

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
    writing_end.sendall(HELLO + b"tail")

    assert read_request(reading_end) == EmptyHeader()
    assert reading_end.recv(64) == b"tail"


@pytest.mark.parametrize("length", [0, 1, 4097, 65535])
def test_a_header_length_outside_two_to_4096_is_refused_before_any_body_read(
    wire: tuple[socket.socket, socket.socket], length: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(b"PBW\x01\x01" + length.to_bytes(2, "big"))

    with pytest.raises(WireRefusedError):
        read_request(reading_end)


def test_a_header_of_4096_bytes_is_read_to_its_end_and_judged_by_content(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(b"PBW\x01\x01\x10\x00" + b" " * 4094 + b"{}")

    assert read_request(reading_end) == EmptyHeader()


def step_message(header: bytes, kind: int = 2) -> bytes:
    return b"PBW\x01" + bytes([kind]) + len(header).to_bytes(2, "big") + header


@pytest.mark.parametrize("step", list(StepKind))
def test_a_valid_step_header_is_read_with_its_step_and_slug(
    wire: tuple[socket.socket, socket.socket], step: StepKind
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(step_message(b'{"step":"%s","slug":"my-deck"}' % step.encode()))

    assert read_request(reading_end) == StepHeader(step=step, slug="my-deck")


BAD_STEP_HEADERS = {
    "an extra key": b'{"step":"build","slug":"a","more":1}',
    "a missing slug": b'{"step":"build"}',
    "a missing step": b'{"slug":"a"}',
    "an integer step": b'{"step":1,"slug":"a"}',
    "a null step": b'{"step":null,"slug":"a"}',
    "an integer slug": b'{"step":"build","slug":1}',
    "a null slug": b'{"step":"build","slug":null}',
    "an unknown step": b'{"step":"deploy","slug":"a"}',
    "an empty slug": b'{"step":"build","slug":""}',
    "a dot slug": b'{"step":"build","slug":"."}',
    "a dotdot slug": b'{"step":"build","slug":".."}',
    "a slash in the slug": b'{"step":"build","slug":"a/b"}',
    "a backslash in the slug": b'{"step":"build","slug":"a\\\\b"}',
    "a quote in the slug": b'{"step":"build","slug":"a\\"b"}',
    "a NUL in the slug": b'{"step":"build","slug":"a\\u0000b"}',
    "not UTF-8": b"\xff\xfe",
}


@pytest.mark.parametrize(
    "header", BAD_STEP_HEADERS.values(), ids=BAD_STEP_HEADERS.keys()
)
def test_a_step_header_that_is_not_exactly_a_step_and_a_plain_slug_is_refused(
    wire: tuple[socket.socket, socket.socket], header: bytes
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(step_message(header))

    with pytest.raises(WireRefusedError):
        read_request(reading_end)


@pytest.mark.parametrize("length", [0, 1, 4097])
def test_a_step_header_length_outside_two_to_4096_is_refused_before_any_body_read(
    wire: tuple[socket.socket, socket.socket], length: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(b"PBW\x01\x02" + length.to_bytes(2, "big"))

    with pytest.raises(WireRefusedError):
        read_request(reading_end)


@pytest.mark.parametrize("kind", [3, 4, 5])
def test_a_worker_direction_kind_is_refused(
    wire: tuple[socket.socket, socket.socket], kind: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(step_message(b"{}", kind))

    with pytest.raises(WireRefusedError):
        read_request(reading_end)
