"""The request record's codec and the production seal that freezes it."""

import errno
import fcntl
import os
from collections.abc import Callable, Iterator

import pytest

from presentator.adapters.build_wire import (
    MAXIMUM_HEADER_BYTES,
    StepHeader,
    WireRefusedError,
    encode_record,
    read_record,
    write_record,
)
from presentator.contracts.decks import StepKind
from presentator.host import worker
from presentator.host.worker_serving import create_request_record

# SEAL, SHRINK, GROW and WRITE as linux/fcntl.h defines them.
KERNEL_ALL_FOUR_SEALS = 0b1111
VALID_RECORD = b'{"step":"build","slug":"my-deck"}'
REFUSED_RECORDS = {
    "empty": b"",
    "truncated": VALID_RECORD[:-3],
    "noise": b"\x00\xff\xfenot json",
    "not UTF-8": b'{"step":"build","slug":"\xff"}',
    "an unknown step": b'{"step":"deploy","slug":"my-deck"}',
    "slug ..": b'{"step":"build","slug":".."}',
    "a slug with a slash": b'{"step":"build","slug":"a/b"}',
    "an extra key": b'{"step":"build","slug":"a","more":1}',
    "4097 bytes of valid JSON": VALID_RECORD.ljust(MAXIMUM_HEADER_BYTES + 1),
}


@pytest.fixture
def record() -> Iterator[int]:
    descriptor = create_request_record()
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("step", list(StepKind))
def test_a_written_record_reads_back_as_the_same_header(
    record: int, step: StepKind
) -> None:
    header = StepHeader(step=step, slug="my-deck")

    write_record(record, header)

    assert read_record(record) == header
    assert os.pread(record, MAXIMUM_HEADER_BYTES, 0) == encode_record(header)
    assert os.lseek(record, 0, os.SEEK_CUR) == 0


def test_a_record_of_exactly_4096_bytes_is_read(record: int) -> None:
    os.pwrite(record, VALID_RECORD.ljust(MAXIMUM_HEADER_BYTES), 0)

    assert read_record(record) == StepHeader(step=StepKind.BUILD, slug="my-deck")


@pytest.mark.parametrize("forged", REFUSED_RECORDS.values(), ids=REFUSED_RECORDS.keys())
def test_a_record_that_is_not_a_valid_header_is_refused(
    record: int, forged: bytes
) -> None:
    os.pwrite(record, forged, 0)

    with pytest.raises(WireRefusedError):
        read_record(record)


def test_the_production_seal_sets_all_four_seals_on_a_fresh_memfd(record: int) -> None:
    worker.seal_record(record)

    assert fcntl.fcntl(record, worker.F_GET_SEALS) == KERNEL_ALL_FOUR_SEALS


TAMPERS: dict[str, Callable[[int], object]] = {
    "write": lambda writable: os.pwrite(writable, b"x", 0),
    "grow": lambda writable: os.ftruncate(writable, 1 << 16),
    "shrink": lambda writable: os.ftruncate(writable, 1),
}


@pytest.mark.parametrize("tamper", TAMPERS.values(), ids=TAMPERS.keys())
def test_after_the_seal_a_writable_duplicate_can_neither_write_grow_nor_shrink(
    record: int, tamper: Callable[[int], object]
) -> None:
    write_record(record, StepHeader(step=StepKind.BUILD, slug="my-deck"))
    writable = os.dup(record)
    try:
        worker.seal_record(record)

        with pytest.raises(PermissionError) as refused:
            tamper(writable)
    finally:
        os.close(writable)

    assert refused.value.errno == errno.EPERM
    assert os.pread(record, MAXIMUM_HEADER_BYTES, 0) == VALID_RECORD
