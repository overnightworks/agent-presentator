"""The codec's client half, read by the server half and answering it."""

import os
import socket
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from presentator.adapters.build_wire import (
    BuiltOutcome,
    EmptyHeader,
    FailedOutcome,
    StepHeader,
    WireRefusedError,
    read_outcome,
    read_request,
    read_status,
    receive_entries,
    send_failed,
    send_hello,
    send_outcome,
    send_status,
    send_step,
    send_tree,
    send_tree_reply,
)
from presentator.contracts.decks import StepKind

MEGABYTES = 1


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    client, worker = socket.socketpair()
    with closing(client), closing(worker):
        yield client, worker


def open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def test_a_hello_is_read_by_the_worker_as_the_empty_header(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair

    send_hello(client)

    assert read_request(worker) == EmptyHeader()


def test_a_step_is_read_by_the_worker_with_its_step_and_slug(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair
    header = StepHeader(step=StepKind.EXPORT, slug="deck-one")

    send_step(client, header)

    assert read_request(worker) == header


def test_a_status_from_the_worker_is_read_and_anything_else_is_refused(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair
    send_status(worker)
    read_status(client)

    send_outcome(worker, BuiltOutcome())

    with pytest.raises(WireRefusedError):
        read_status(client)


@pytest.mark.parametrize("outcome", [BuiltOutcome(), FailedOutcome(text="boom")])
def test_an_outcome_from_the_worker_is_read_as_the_outcome_it_carries(
    pair: tuple[socket.socket, socket.socket], outcome: BuiltOutcome | FailedOutcome
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair

    send_outcome(worker, outcome)

    assert read_outcome(client) == outcome


def test_a_status_where_the_outcome_is_due_is_refused(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair
    send_status(worker)

    with pytest.raises(WireRefusedError):
        read_outcome(client)


def test_a_failure_the_worker_sends_is_read_with_its_text(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair

    send_failed(worker, "slidev stopped")

    assert read_outcome(client) == FailedOutcome(text="slidev stopped")


def test_an_uploaded_tree_is_received_by_the_worker_below_its_deck(
    pair: tuple[socket.socket, socket.socket], tmp_path: Path
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair
    source, deck = tmp_path / "source", tmp_path / "deck"
    (source / "assets").mkdir(parents=True)
    (source / "slides.md").write_bytes(b"# one\n")
    (source / "assets" / "a.txt").write_bytes(b"a")
    deck.mkdir()
    source_root, deck_root = open_directory(source), open_directory(deck)
    try:
        send_tree(client, source_root)
        receive_entries(worker, deck_root, MEGABYTES)
    finally:
        os.close(source_root)
        os.close(deck_root)

    assert (deck / "slides.md").read_bytes() == b"# one\n"
    assert (deck / "assets" / "a.txt").read_bytes() == b"a"


def test_an_output_tree_answered_built_is_read_by_the_client_and_received(
    pair: tuple[socket.socket, socket.socket], tmp_path: Path
) -> None:
    """#431: the build-wire codec's client half."""
    client, worker = pair
    output, received = tmp_path / "output", tmp_path / "received"
    (output / "talk").mkdir(parents=True)
    (output / "talk" / "index.html").write_bytes(b"<html>")
    received.mkdir()
    output_root, received_root = open_directory(output), open_directory(received)
    try:
        send_tree_reply(worker, output_root, StepKind.BUILD, MEGABYTES)
        outcome = read_outcome(client)
        receive_entries(client, received_root, MEGABYTES)
    finally:
        os.close(output_root)
        os.close(received_root)

    assert outcome == BuiltOutcome()
    assert (received / "talk" / "index.html").read_bytes() == b"<html>"
