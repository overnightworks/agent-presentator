"""The build wire's entry receiver and path rule, over a socket pair and a deck."""

import os
import socket
import threading
import tracemalloc
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from presentator.adapters.build_wire import (
    LENGTH,
    MAXIMUM_PATH_BYTES,
    SIZE,
    RecordTag,
    WireRefusedError,
    path_components,
    receive_entries,
)
from tests.worker_support import WAIT_SECONDS, directory_entry, end_entry, file_entry

SENDER_JOIN_SECONDS = 10
SHORT_RECEIVE_BYTES = 4096
MEBIBYTE = 1024 * 1024
DEEPEST = 32


class RecordingSocket(socket.socket):
    """A socket that logs each recv request and the payload backlog after it."""

    def __init__(
        self, fileno: int, deck: Path, header_bytes: int, payload: int
    ) -> None:
        """Wrap the descriptor; the deck, header and payload sizes serve the backlog."""
        super().__init__(fileno=fileno)
        self.requests: list[int] = []
        self.backlog_after_each_recv: list[int] = []
        self._received = 0
        self._deck = deck
        self._header_bytes = header_bytes
        self._payload = payload

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        self.requests.append(bufsize)
        piece = super().recv(min(bufsize, SHORT_RECEIVE_BYTES), flags)
        self._received += len(piece)
        payload_received = min(
            max(self._received - self._header_bytes, 0), self._payload
        )
        written = sum(path.stat().st_size for path in self._deck.iterdir())
        self.backlog_after_each_recv.append(payload_received - written)
        return piece


def deck_tree(tmp_path: Path) -> dict[bytes, bytes | None]:
    """Every path below the deck, with a file's content or None for a directory."""
    root = tmp_path / "deck"
    return {
        os.fsencode(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None
        )
        for path in root.rglob("*")
    }


def open_descriptors() -> set[Path]:
    return set(Path("/proc/self/fd").iterdir())


def receive_sent(
    wire: tuple[socket.socket, socket.socket], deck: int, sent: bytes
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(sent)
    writing_end.shutdown(socket.SHUT_WR)
    receive_entries(reading_end, deck)


NESTED = [b"/".join([b"a"] * depth) for depth in range(1, DEEPEST + 1)]
DEEPEST_DIRECTORIES = b"".join(directory_entry(path) for path in NESTED)
TOO_DEEP = b"/".join([b"a"] * (DEEPEST + 1))
ENTRY_MAKERS: dict[str, Callable[[bytes], bytes]] = {
    "DIR": directory_entry,
    "FILE": lambda path: file_entry(path, b""),
}
REFUSED_PATHS = {
    "an empty path": b"",
    "a leading slash": b"/a",
    "an empty component": b"a//b",
    "a trailing slash": b"a/",
    "a dot": b".",
    "a dot component": b"a/./b",
    "dot dot": b"..",
    "a trailing dot dot": b"a/..",
    "a NUL": b"a\0b",
    "a component of 256 bytes": b"x" * 256,
    "a path of 1025 bytes": b"a/" * 512 + b"a",
    "depth 33": TOO_DEEP,
}


@pytest.mark.parametrize("make", ENTRY_MAKERS.values(), ids=ENTRY_MAKERS.keys())
@pytest.mark.parametrize("path", REFUSED_PATHS.values(), ids=REFUSED_PATHS.keys())
def test_a_path_that_breaks_the_rule_is_refused_and_never_created(
    wire: tuple[socket.socket, socket.socket],
    deck: int,
    tmp_path: Path,
    path: bytes,
    make: Callable[[bytes], bytes],
) -> None:
    sent = DEEPEST_DIRECTORIES + make(path)

    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)

    assert set(deck_tree(tmp_path)) == set(NESTED)


def test_a_path_length_of_zero_is_refused(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    sent = bytes([RecordTag.FILE]) + LENGTH.pack(0)

    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)


def test_a_path_length_over_the_maximum_is_refused_without_waiting_for_its_bytes(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(bytes([RecordTag.DIR]) + LENGTH.pack(1025))

    with pytest.raises(WireRefusedError):
        receive_entries(reading_end, deck)


def test_an_unknown_tag_after_a_valid_entry_is_refused_without_waiting(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    reading_end, writing_end = wire
    writing_end.sendall(directory_entry(b"a") + b"\x00")

    with pytest.raises(WireRefusedError):
        receive_entries(reading_end, deck)

    assert deck_tree(tmp_path) == {b"a": None}


def test_the_boundary_names_and_depths_are_accepted_under_their_raw_bytes(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    longest = [b"a" * 255] * 3 + [b"b" * 254, b"c"]
    prefixes = [b"/".join(longest[:end]) for end in range(1, len(longest))]
    full = b"/".join(longest)
    assert len(full) == MAXIMUM_PATH_BYTES
    sent = (
        DEEPEST_DIRECTORIES
        + b"".join(directory_entry(prefix) for prefix in prefixes)
        + file_entry(full, b"1")
        + file_entry(b"z" * 255, b"3")
        + file_entry(b"\xff", b"2")
        + end_entry(DEEPEST + len(prefixes) + 3, 3)
    )

    receive_sent(wire, deck, sent)

    tree = deck_tree(tmp_path)
    assert tree[full] == b"1"
    assert tree[b"\xff"] == b"2"
    assert tree[b"z" * 255] == b"3"
    assert tree[NESTED[-1]] is None


ORDER_FAULTS = {
    "unsorted": (file_entry(b"b", b"") + file_entry(b"a", b""), {b"b"}),
    "a repeated file": (file_entry(b"a", b"") * 2, {b"a"}),
    "a repeated directory": (directory_entry(b"a") * 2, {b"a"}),
    "an orphan below a missing directory": (file_entry(b"a/x", b""), set[bytes]()),
    "an orphan below a file": (
        file_entry(b"f", b"") + file_entry(b"f/x", b""),
        {b"f"},
    ),
    "a re-entered directory": (
        directory_entry(b"a")
        + file_entry(b"a/x", b"")
        + file_entry(b"b", b"")
        + file_entry(b"a/y", b""),
        {b"a", b"a/x", b"b"},
    ),
}


@pytest.mark.parametrize(
    ("sent", "kept"), ORDER_FAULTS.values(), ids=ORDER_FAULTS.keys()
)
def test_an_entry_out_of_order_is_refused_before_it_is_created(
    wire: tuple[socket.socket, socket.socket],
    deck: int,
    tmp_path: Path,
    sent: bytes,
    kept: set[bytes],
) -> None:
    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)

    assert set(deck_tree(tmp_path)) == kept


END_MISMATCHES = {
    "one entry too many": end_entry(2, 1),
    "one entry too few": end_entry(0, 1),
    "a total one too high": end_entry(1, 2),
    "a total one too low": end_entry(1, 0),
}


@pytest.mark.parametrize("end", END_MISMATCHES.values(), ids=END_MISMATCHES.keys())
def test_an_end_that_does_not_match_what_was_received_is_refused(
    wire: tuple[socket.socket, socket.socket], deck: int, end: bytes
) -> None:
    sent = file_entry(b"a", b"x") + end

    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)


def test_end_of_file_inside_a_payload_is_refused(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    sent = file_entry(b"a", b"abc")[:-1]

    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)


@pytest.mark.parametrize("make", ENTRY_MAKERS.values(), ids=ENTRY_MAKERS.keys())
def test_a_top_level_node_modules_is_refused_before_it_is_created(
    wire: tuple[socket.socket, socket.socket],
    deck: int,
    tmp_path: Path,
    make: Callable[[bytes], bytes],
) -> None:
    sent = make(b"node_modules")

    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)

    assert deck_tree(tmp_path) == {}


def test_a_nested_node_modules_is_accepted(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    receive_sent(
        wire,
        deck,
        directory_entry(b"talk")
        + directory_entry(b"talk/node_modules")
        + end_entry(2, 0),
    )

    assert deck_tree(tmp_path) == {b"talk": None, b"talk/node_modules": None}


def test_a_nested_upload_is_stored_byte_equal_and_the_read_stops_after_end(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    reading_end, writing_end = wire
    big = os.urandom(200 * 1024)
    sent = (
        directory_entry(b"a")
        + directory_entry(b"a/b")
        + file_entry(b"a/b/c", b"deep")
        + file_entry(b"a/d", b"")
        + file_entry(b"e", big)
        + end_entry(5, 4 + len(big))
        + b"next"
    )
    sender = threading.Thread(target=writing_end.sendall, args=(sent,))
    sender.start()
    try:
        receive_entries(reading_end, deck)
        following = reading_end.recv(4)
    finally:
        sender.join(SENDER_JOIN_SECONDS)

    assert not sender.is_alive()
    assert following == b"next"
    assert deck_tree(tmp_path) == {
        b"a": None,
        b"a/b": None,
        b"a/b/c": b"deep",
        b"a/d": b"",
        b"e": big,
    }


def test_a_symlink_in_the_deck_is_never_followed(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    (tmp_path / "outside").mkdir()
    (tmp_path / "deck/link").symlink_to(tmp_path / "outside")

    sent = directory_entry(b"link") + end_entry(1, 0)

    with pytest.raises(FileExistsError):
        receive_sent(wire, deck, sent)

    assert list((tmp_path / "outside").iterdir()) == []


def test_a_successful_upload_leaves_the_root_open_and_closes_what_it_opened(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    before = open_descriptors()

    receive_sent(
        wire,
        deck,
        directory_entry(b"a") + directory_entry(b"a/b") + end_entry(2, 0),
    )

    assert os.fstat(deck)
    assert open_descriptors() == before


def test_a_refusal_three_levels_deep_closes_every_descriptor_but_the_root(
    wire: tuple[socket.socket, socket.socket], deck: int
) -> None:
    before = open_descriptors()
    sent = (
        directory_entry(b"a")
        + directory_entry(b"a/b")
        + file_entry(b"a/b/y", b"")
        + file_entry(b"a/b/x", b"")
    )

    with pytest.raises(WireRefusedError):
        receive_sent(wire, deck, sent)

    assert os.fstat(deck)
    assert open_descriptors() == before


def test_an_oserror_closes_every_descriptor_but_the_root_and_surfaces(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    (tmp_path / "deck/a").write_bytes(b"in the way")
    before = open_descriptors()

    sent = file_entry(b"a", b"x")

    with pytest.raises(FileExistsError):
        receive_sent(wire, deck, sent)

    assert os.fstat(deck)
    assert open_descriptors() == before


def test_a_payload_is_received_in_chunks_and_written_before_the_next_is_read(
    wire: tuple[socket.socket, socket.socket], tmp_path: Path
) -> None:
    (tmp_path / "deck").mkdir()
    reading_end, writing_end = wire
    content = os.urandom(200 * 1024)
    sent = file_entry(b"big", content) + end_entry(1, len(content))
    header_bytes = len(file_entry(b"big", b""))
    recording = RecordingSocket(
        reading_end.detach(), tmp_path / "deck", header_bytes, len(content)
    )
    recording.settimeout(WAIT_SECONDS)
    root = os.open(tmp_path / "deck", os.O_RDONLY | os.O_DIRECTORY)
    sender = threading.Thread(target=writing_end.sendall, args=(sent,))
    sender.start()
    try:
        with closing(recording):
            receive_entries(recording, root)
    finally:
        sender.join(SENDER_JOIN_SECONDS)
        os.close(root)

    assert not sender.is_alive()
    assert (tmp_path / "deck/big").read_bytes() == content
    assert max(recording.requests) <= 64 * 1024
    assert max(recording.backlog_after_each_recv) <= 64 * 1024


def test_a_declared_size_far_beyond_the_payload_is_refused_without_allocating_it(
    wire: tuple[socket.socket, socket.socket], deck: int, tmp_path: Path
) -> None:
    sent = (
        bytes([RecordTag.FILE])
        + LENGTH.pack(1)
        + b"f"
        + SIZE.pack(2**64 - 1)
        + bytes(10)
    )
    before = open_descriptors()

    tracemalloc.start()
    try:
        with pytest.raises(WireRefusedError):
            receive_sent(wire, deck, sent)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < MEBIBYTE
    assert (tmp_path / "deck/f").read_bytes() == b""
    assert os.fstat(deck)
    assert open_descriptors() == before


@pytest.mark.parametrize(
    ("raw", "components"),
    [(b"a", (b"a",)), (b"a/b\xff", (b"a", b"b\xff"))],
)
def test_a_valid_path_splits_on_slashes_into_raw_components(
    raw: bytes, components: tuple[bytes, ...]
) -> None:
    assert path_components(raw) == components


@pytest.mark.parametrize("path", REFUSED_PATHS.values(), ids=REFUSED_PATHS.keys())
def test_the_path_rule_refuses_every_broken_path(path: bytes) -> None:
    with pytest.raises(WireRefusedError):
        path_components(path)
