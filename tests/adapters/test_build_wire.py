"""The build wire's framing: what a HELLO or STEP may look like and what STATUS is."""

import json
import socket

import pytest

from presentator.adapters.build_wire import (
    EmptyHeader,
    RecordTag,
    StepHeader,
    WireRefusedError,
    read_request,
    receive_entries,
    send_end,
    send_failed,
    send_status,
)
from presentator.contracts.decks import StepKind
from tests import worker_support as support


def test_status_is_the_wire_format_bytes(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire

    send_status(writing_end)

    assert reading_end.recv(64) == support.STATUS


def test_failed_is_an_outcome_message_with_a_null_text(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire

    send_failed(writing_end)

    assert reading_end.recv(64) == support.OUTCOME_FAILED


def test_an_end_without_entries_is_thirteen_bytes(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire

    send_end(writing_end)

    assert reading_end.recv(64) == support.END


def test_a_hello_with_the_empty_header_is_read(
    wire: tuple[socket.socket, socket.socket],
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(support.HELLO + b"tail")

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


STEP_SLUGS = {
    "a plain slug": "my-deck",
    "a unicode slug": "décor",
    "a mixed script slug": "décor-漢字",
    "a slug filling the longest step header to 4096 bytes": "a"
    * (4096 - len(b'{"step":"export","slug":""}')),
}


@pytest.mark.parametrize("slug", STEP_SLUGS.values(), ids=STEP_SLUGS.keys())
@pytest.mark.parametrize("step", list(StepKind))
def test_a_valid_step_header_is_read_with_its_step_and_slug(
    wire: tuple[socket.socket, socket.socket], step: StepKind, slug: str
) -> None:
    reading_end, writing_end = wire
    header = json.dumps(
        {"step": step.value, "slug": slug}, ensure_ascii=False, separators=(",", ":")
    )
    writing_end.sendall(support.framed(header.encode(), support.STEP))

    assert read_request(reading_end) == StepHeader(step=step, slug=slug)


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
    writing_end.sendall(support.framed(header, support.STEP))

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
    writing_end.sendall(support.framed(b"{}", b"PBW\x01" + bytes([kind])))

    with pytest.raises(WireRefusedError):
        read_request(reading_end)


def test_a_thirteen_byte_end_with_no_entries_is_accepted_and_read_no_further(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(support.END + b"more")

    receive_entries(reading_end, deck, 300)

    assert reading_end.recv(64) == b"more"


END_REFUSALS = {
    "a short read": b"\x03" + bytes(11),
    "no bytes": b"",
    "a directory tag with no path": bytes([RecordTag.DIR]) + bytes(12),
    "a file tag with no path": bytes([RecordTag.FILE]) + bytes(12),
    "an unknown tag": b"\x00" + bytes(12),
    "a non-zero count": b"\x03\x00\x00\x00\x01" + bytes(8),
    "a non-zero sum": b"\x03" + bytes(11) + b"\x01",
}


@pytest.mark.parametrize("sent", END_REFUSALS.values(), ids=END_REFUSALS.keys())
def test_an_end_that_is_short_wrongly_tagged_or_not_empty_is_refused(
    wire: tuple[socket.socket, socket.socket], deck: int, sent: bytes
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(sent)
    writing_end.shutdown(socket.SHUT_WR)

    with pytest.raises(WireRefusedError):
        receive_entries(reading_end, deck, 300)


def test_a_lone_unknown_tag_is_refused_without_waiting_for_the_rest(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(b"\x00")

    with pytest.raises(WireRefusedError):
        receive_entries(reading_end, deck, 300)
