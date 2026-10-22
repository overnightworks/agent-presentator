"""The sender's walk of /work/out: what it admits, refuses, limits and sends."""

import errno
import os
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from presentator.adapters.build_wire import (
    MEGABYTE,
    WireRefusedError,
    receive_entries,
    send_tree_reply,
)
from presentator.contracts.decks import StepKind
from tests import worker_support as support

BUILT = support.OUTCOME_BUILT
MEGABYTES = 1
type OpenablePath = str | bytes | os.PathLike[str] | os.PathLike[bytes]

AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores file modes, so a 0000 mode refuses nothing"
)


def refused(limit: str, written: int | None = None) -> bytes:
    """The OUTCOME refused message followed by an END without entries."""
    header = b'{"outcome":"refused","limit":"%s","written":%s}' % (
        limit.encode(),
        b"null" if written is None else b"%d" % written,
    )
    return support.framed(header, b"PBW\x01\x05") + support.END


def reply_to(output: Path, step: StepKind, megabytes: int = MEGABYTES) -> bytes:
    """Everything the sender writes for the tree, read until it closes."""
    reading_end, writing_end = socket.socketpair()
    received: list[bytes] = []

    def read_all() -> None:
        while chunk := reading_end.recv(1 << 16):
            received.append(chunk)

    reader = threading.Thread(target=read_all)
    reader.start()
    root = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        send_tree_reply(writing_end, root, step, megabytes)
    finally:
        os.close(root)
        writing_end.close()
        reader.join()
        reading_end.close()
    return b"".join(received)


@pytest.fixture
def output(tmp_path: Path) -> Iterator[Path]:
    """The empty /work/out of a finished step; unreadable cases are opened to clean."""
    (tmp_path / "out").mkdir()
    yield tmp_path / "out"
    for path in (tmp_path / "out").rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o755)


def build_talk(output: Path) -> Path:
    (output / "talk").mkdir()
    return output / "talk"


def a_symlink_to_a_file(talk: Path) -> None:
    (talk / "index.html").write_text("x")
    (talk / "link").symlink_to("index.html")


def a_symlink_to_a_directory(talk: Path) -> None:
    (talk / "assets").mkdir()
    (talk / "link").symlink_to("assets")


def a_fifo(talk: Path) -> None:
    os.mkfifo(talk / "pipe")


def a_socket(talk: Path) -> None:
    with socket.socket(socket.AF_UNIX) as bound:
        bound.bind(str(talk / "socket"))


def a_hardlinked_file(talk: Path) -> None:
    (talk / "a").write_text("x")
    os.link(talk / "a", talk / "b")


def an_unreadable_file(talk: Path) -> None:
    (talk / "secret").write_text("x")
    (talk / "secret").chmod(0)


def an_unreadable_directory(talk: Path) -> None:
    (talk / "closed").mkdir()
    (talk / "closed").chmod(0)


def a_directory_without_search_permission(talk: Path) -> None:
    (talk / "closed").mkdir()
    (talk / "closed" / "f").write_text("x")
    (talk / "closed").chmod(0o644)


def a_path_at_depth_33(talk: Path) -> None:
    deepest = talk
    for _ in range(31):
        deepest = deepest / "d"
        deepest.mkdir()
    (deepest / "f").write_text("x")


def a_path_over_1024_bytes(talk: Path) -> None:
    deepest = talk
    for _ in range(6):
        deepest = deepest / ("d" * 200)
        deepest.mkdir()
    (deepest / "f").write_text("x")


UNSAFE_TREES = [
    pytest.param(a_symlink_to_a_file, id="a symlink to a file"),
    pytest.param(a_symlink_to_a_directory, id="a symlink to a directory"),
    pytest.param(a_fifo, id="a FIFO"),
    pytest.param(a_socket, id="a socket"),
    pytest.param(a_hardlinked_file, id="a hardlinked file"),
    pytest.param(an_unreadable_file, id="a 0000-mode file", marks=AS_ROOT),
    pytest.param(an_unreadable_directory, id="a 0000-mode directory", marks=AS_ROOT),
    pytest.param(
        a_directory_without_search_permission,
        id="a directory without search permission",
        marks=AS_ROOT,
    ),
    pytest.param(a_path_at_depth_33, id="a path at depth 33"),
    pytest.param(a_path_over_1024_bytes, id="a path over 1024 bytes"),
]


@pytest.mark.parametrize("make", UNSAFE_TREES)
def test_an_unsafe_entry_refuses_the_whole_build_as_shape_without_any_entry(
    output: Path, make: Callable[[Path], None]
) -> None:
    """Cell W1 (29)."""
    make(build_talk(output))

    assert reply_to(output, StepKind.BUILD) == refused("shape")


def test_a_fifo_is_refused_without_ever_being_opened(
    output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cell W1 (29): the entry's type is judged before any open."""
    a_fifo(build_talk(output))
    opened: list[OpenablePath] = []
    open_entry = os.open

    def recording_open(path: OpenablePath, *arguments: int, **keywords: int) -> int:
        opened.append(path)
        return open_entry(path, *arguments, **keywords)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "open", recording_open)

    assert reply_to(output, StepKind.BUILD) == refused("shape")
    assert not [path for path in opened if os.fsencode(path).split(b"/")[-1] == b"pipe"]


def build_without_talk(output: Path) -> None:
    (output / "other").mkdir()


def build_with_talk_as_a_file(output: Path) -> None:
    (output / "talk").write_text("x")


def build_with_a_file_beside_talk(output: Path) -> None:
    (build_talk(output) / "index.html").write_text("x")
    (output / "zeta.txt").write_text("x")


def build_with_a_directory_before_talk(output: Path) -> None:
    (output / "a").mkdir()
    build_talk(output)


def export_with_a_file_below_talk(output: Path) -> None:
    (build_talk(output) / "x").write_text("x")


def export_with_a_second_file(output: Path) -> None:
    (output / "deck.pdf").write_text("x")
    (output / "other").write_text("x")


def export_with_a_directory_as_the_pdf(output: Path) -> None:
    (output / "deck.pdf").mkdir()


CONTRACT_BREAKS: dict[str, tuple[StepKind, Callable[[Path], None]]] = {
    "a build without talk": (StepKind.BUILD, build_without_talk),
    "a build with talk as a file": (StepKind.BUILD, build_with_talk_as_a_file),
    "a build with a top-level file beside talk": (
        StepKind.BUILD,
        build_with_a_file_beside_talk,
    ),
    "a build with a directory before talk": (
        StepKind.BUILD,
        build_with_a_directory_before_talk,
    ),
    "a build with an empty out": (StepKind.BUILD, lambda _output: None),
    "an export with talk/x": (StepKind.EXPORT, export_with_a_file_below_talk),
    "an export with a second file": (StepKind.EXPORT, export_with_a_second_file),
    "an export with deck.pdf as a directory": (
        StepKind.EXPORT,
        export_with_a_directory_as_the_pdf,
    ),
    "an export with an empty out": (StepKind.EXPORT, lambda _output: None),
}


@pytest.mark.parametrize(
    ("step", "make"), CONTRACT_BREAKS.values(), ids=CONTRACT_BREAKS.keys()
)
def test_an_output_outside_the_step_contract_is_refused_as_shape(
    output: Path, step: StepKind, make: Callable[[Path], None]
) -> None:
    """Cell W2 (30)."""
    make(output)

    assert reply_to(output, step) == refused("shape")


def sparse_talk_file(output: Path, size: int) -> None:
    with (build_talk(output) / "blob").open("wb") as blob:
        blob.truncate(size)


def test_a_total_of_exactly_the_megabytes_is_built(output: Path) -> None:
    """Cell W3 (31)."""
    sparse_talk_file(output, MEGABYTES * MEGABYTE)

    assert reply_to(output, StepKind.BUILD).startswith(BUILT)


def test_one_byte_over_the_megabytes_is_refused_with_the_rounded_up_total(
    output: Path,
) -> None:
    """Cell W3 (31)."""
    sparse_talk_file(output, MEGABYTES * MEGABYTE + 1)

    assert reply_to(output, StepKind.BUILD) == refused("megabytes", MEGABYTES + 1)


def many_empty_files(output: Path, count: int) -> None:
    """A talk directory with the files that make count entries including itself."""
    talk = build_talk(output)
    for number in range(count - 1):
        os.close(os.open(talk / f"{number:06d}", os.O_CREAT | os.O_WRONLY))


def test_one_hundred_thousand_entries_are_built(output: Path) -> None:
    """Cell W3 (31)."""
    many_empty_files(output, 100_000)

    assert reply_to(output, StepKind.BUILD).startswith(BUILT)


def test_one_entry_over_one_hundred_thousand_is_refused_as_entries(
    output: Path,
) -> None:
    """Cell W3 (31)."""
    many_empty_files(output, 100_001)

    assert reply_to(output, StepKind.BUILD) == refused("entries")


def entry_stream(reply: bytes) -> bytes:
    assert reply.startswith(BUILT)
    return reply[len(BUILT) :]


def received_into_an_empty_directory(entries: bytes, destination: Path) -> None:
    reading_end, writing_end = socket.socketpair()
    sender = threading.Thread(
        target=lambda: (writing_end.sendall(entries), writing_end.close())
    )
    sender.start()
    deck = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        receive_entries(reading_end, deck, MEGABYTES)
    finally:
        os.close(deck)
        sender.join()
        reading_end.close()


def tree_of(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def test_a_nested_build_is_sent_in_byte_order_and_received_as_the_same_tree(
    output: Path, tmp_path: Path
) -> None:
    """Cell W4 (35)."""
    talk = build_talk(output)
    (talk / "index.html").write_bytes(b"<html>")
    (talk / "assets").mkdir()
    (talk / "assets" / "app.js").write_bytes(b"x" * 200_000)
    (talk / "assets" / "B.css").write_bytes(b"b")
    (talk / "assets" / "é.png").write_bytes(b"e")
    (talk / "Z").write_bytes(b"")
    (talk / "empty").mkdir()
    entries = entry_stream(reply_to(output, StepKind.BUILD))
    destination = tmp_path / "received"
    destination.mkdir()

    received_into_an_empty_directory(entries, destination)

    assert tree_of(destination) == tree_of(output)
    assert entries.endswith(support.end_entry(8, 200_000 + 6 + 1 + 1))
    assert entries.startswith(support.directory_entry(b"talk"))


def test_an_export_sends_its_pdf_and_nothing_else(output: Path) -> None:
    """Cell W4 (35)."""
    (output / "deck.pdf").write_bytes(b"%PDF")

    entries = entry_stream(reply_to(output, StepKind.EXPORT))

    assert entries == support.file_entry(b"deck.pdf", b"%PDF") + support.end_entry(1, 4)


def test_a_lookup_failing_for_any_other_reason_is_not_judged_as_shape(
    output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 1: any other OSError passes through, with no OUTCOME sent."""
    (build_talk(output) / "index.html").write_text("x")

    def failing_stat(*_arguments: object, **_options: object) -> os.stat_result:
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "stat", failing_stat)

    with pytest.raises(OSError, match="input/output error"):
        reply_to(output, StepKind.BUILD)


def test_a_file_that_ends_before_its_surveyed_size_aborts_the_reply(
    output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 1: a short read raises, and the reply ends without END."""
    (build_talk(output) / "index.html").write_text("x")

    def reading_nothing(_descriptor: int, _length: int) -> bytes:
        return b""

    monkeypatch.setattr(os, "read", reading_nothing)

    with pytest.raises(WireRefusedError):
        reply_to(output, StepKind.BUILD)
