"""The build worker client over a socket pair with the reply already sent."""

import errno
import logging
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from presentator.adapters.build_wire import MAXIMUM_ENTRIES, RefusalLimit
from presentator.adapters.build_worker import (
    OUTPUT_SHAPE_FOR_A_PAGE,
    exchange_with_worker,
)
from presentator.adapters.builds import (
    TOO_MANY_FOR_A_PAGE,
    TOO_MUCH_FOR_A_PAGE,
    BuildStep,
)
from presentator.contracts.decks import (
    DECK_DIRECTORY,
    FAILURE_TEXT_LIMIT,
    OUTPUT_DIRECTORY,
    PDF_FILE,
    TALK_DIRECTORY,
    BuildFailure,
    StepKind,
)
from tests import worker_support as support
from tests.adapters.build_worker_support import (
    BUILT_PDF,
    BUILT_TALK,
    INDEX,
    MEGABYTES,
    WITHOUT_A_REASON,
    a_step,
    failed_reply,
    refused_reply,
    staging_of,
    toolchain,
    tree_of,
)


def exchanged_with(
    step: BuildStep,
    reply: bytes,
    remove_staging: Callable[[Path], None] | None = None,
) -> BuildFailure | None:
    """The result of an exchange with a worker that has already sent its reply."""
    ours, theirs = socket.socketpair()
    theirs.sendall(reply)
    theirs.shutdown(socket.SHUT_WR)
    deck = os.open(step.place.here / DECK_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return exchange_with_worker(
            ours,
            step=step,
            deck=deck,
            megabytes=MEGABYTES,
            remove_staging=remove_staging or toolchain(Path("/")).remove_staging,
        )
    finally:
        os.close(deck)
        ours.close()
        theirs.close()


BAD_REPLIES = [
    pytest.param(b"nonsense", id="a reply that is not the wire"),
    pytest.param(b"", id="end of file before the outcome"),
    pytest.param(support.OUTCOME_BUILT, id="end of file before the entries"),
    pytest.param(
        support.OUTCOME_BUILT + support.talk_entries(INDEX) + b"x", id="a trailing byte"
    ),
    pytest.param(support.OUTCOME_FAILED, id="a failed outcome without END"),
    pytest.param(failed_reply(None) + b"x", id="a failed outcome and a trailing byte"),
    pytest.param(
        support.OUTCOME_FAILED + support.file_entry(b"x", b"") + support.END,
        id="an entry after a failed outcome",
    ),
    pytest.param(
        support.OUTCOME_FAILED + support.end_entry(1, 0), id="a counted failed END"
    ),
    pytest.param(refused_reply(RefusalLimit.MEGABYTES), id="megabytes without written"),
    pytest.param(refused_reply(RefusalLimit.ENTRIES, 3), id="written on another limit"),
    pytest.param(
        support.framed(
            b'{"outcome":"failed","text":"%s"}' % (b"x" * 2001), b"PBW\x01\x05"
        )
        + support.END,
        id="a text past its bound",
    ),
    pytest.param(
        support.OUTCOME_BUILT
        + support.directory_entry(b"talk")
        + support.end_entry(2, 0),
        id="an END counting what was not sent",
    ),
]


@pytest.mark.parametrize("reply", BAD_REPLIES)
def test_a_reply_breaking_the_grammar_fails_and_removes_its_staging(
    tmp_path: Path, reply: bytes
) -> None:
    """Cell B5."""
    step = a_step(tmp_path)

    result = exchanged_with(step, reply)

    assert result == WITHOUT_A_REASON
    assert not staging_of(step).exists()
    assert not list((step.place.here / OUTPUT_DIRECTORY).iterdir())


def test_an_export_reply_carrying_a_talk_leaves_the_built_talk_unchanged(
    tmp_path: Path,
) -> None:
    """Cell B6."""
    step = a_step(tmp_path, StepKind.EXPORT)
    talk = step.place.here / OUTPUT_DIRECTORY / TALK_DIRECTORY
    talk.mkdir()
    (talk / "index.html").write_bytes(INDEX)
    before = tree_of(talk)
    reply = (
        support.OUTCOME_BUILT
        + support.directory_entry(b"talk")
        + support.file_entry(b"talk/x", b"x")
        + support.end_entry(2, 1)
    )

    result = exchanged_with(step, reply)

    assert result == WITHOUT_A_REASON
    assert tree_of(talk) == before
    assert not (step.place.here / OUTPUT_DIRECTORY / PDF_FILE).exists()


def test_a_build_reply_carrying_the_pdf_is_refused(tmp_path: Path) -> None:
    """Cell B6."""
    step = a_step(tmp_path)

    assert exchanged_with(step, BUILT_PDF) == WITHOUT_A_REASON
    assert not list((step.place.here / OUTPUT_DIRECTORY).iterdir())


def test_a_target_already_in_out_is_not_replaced(tmp_path: Path) -> None:
    """Cell B7: the rename target exists."""
    step = a_step(tmp_path)
    planted = step.place.here / OUTPUT_DIRECTORY / TALK_DIRECTORY
    planted.mkdir()
    (planted / "mine").write_text("mine")

    assert exchanged_with(step, BUILT_TALK) == WITHOUT_A_REASON
    assert tree_of(planted) == {"mine": b"mine"}


def test_a_receiver_that_runs_out_of_space_fails_without_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cell B7: ENOSPC while the staged file is written."""
    step = a_step(tmp_path)
    write = os.write

    def writing_to_a_full_disk(descriptor: int, data: bytes | memoryview) -> int:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.ENOSPC, "no space left on device")
        return write(descriptor, data)

    monkeypatch.setattr(os, "write", writing_to_a_full_disk)

    assert exchanged_with(step, BUILT_TALK) == WITHOUT_A_REASON
    assert not staging_of(step).exists()


def test_a_staging_that_cannot_be_removed_leaves_the_result_and_logs_the_fault(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cell B8."""
    step = a_step(tmp_path)

    def failing_to_remove(_staged: Path) -> None:
        raise OSError(errno.EBUSY, "device or resource busy")

    with caplog.at_level(logging.WARNING):
        result = exchanged_with(step, BUILT_TALK, failing_to_remove)

    assert result is None
    assert (
        step.place.here / OUTPUT_DIRECTORY / TALK_DIRECTORY / "index.html"
    ).read_bytes() == INDEX
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_a_staging_left_over_from_before_is_a_fault_and_is_not_removed(
    tmp_path: Path,
) -> None:
    """Fault table: receiver OSError, a staging that is already there."""
    step = a_step(tmp_path)
    staging_of(step).mkdir()

    assert exchanged_with(step, BUILT_TALK) == WITHOUT_A_REASON
    assert staging_of(step).is_dir()


@pytest.mark.parametrize(
    ("reply", "text"),
    [
        pytest.param(
            refused_reply(RefusalLimit.MEGABYTES, 2),
            TOO_MUCH_FOR_A_PAGE.format(written=2, allowed=MEGABYTES),
            id="megabytes",
        ),
        pytest.param(
            refused_reply(RefusalLimit.ENTRIES),
            TOO_MANY_FOR_A_PAGE.format(allowed=MAXIMUM_ENTRIES),
            id="entries",
        ),
        pytest.param(
            refused_reply(RefusalLimit.SHAPE), OUTPUT_SHAPE_FOR_A_PAGE, id="shape"
        ),
        pytest.param(failed_reply("it broke"), "it broke", id="failed with text"),
        pytest.param(
            failed_reply("e" * FAILURE_TEXT_LIMIT),
            "e" * FAILURE_TEXT_LIMIT,
            id="failed with the longest text",
        ),
        pytest.param(failed_reply(None), None, id="failed without text"),
    ],
)
def test_a_refused_or_failed_reply_is_the_text_the_deck_page_shows(
    tmp_path: Path, reply: bytes, text: str | None
) -> None:
    """Cell B9."""
    step = a_step(tmp_path)

    assert exchanged_with(step, reply) == BuildFailure(text=text)
    assert not staging_of(step).exists()
