"""The probe's crafted uploads are refused at the byte their class names."""

import importlib
import os
import socket
import sys
import threading
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

from presentator.adapters.build_wire import WireRefusedError, receive_entries
from presentator.host.worker_serving import LifeEnd

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SENTINEL = b"sentinel"
MEGABYTES = 1


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    return importlib.import_module("probe_build_worker_uploads")


def receive(sent: bytes, deck: Path) -> bytes | None:
    """Receive `sent` into the deck; return the unread bytes if it was refused."""
    deck.mkdir()
    receiver, sender = socket.socketpair()
    with closing(receiver), closing(sender):
        writer = threading.Thread(
            target=lambda: (sender.sendall(sent), sender.shutdown(socket.SHUT_WR))
        )
        writer.start()
        descriptor = os.open(deck, os.O_RDONLY | os.O_DIRECTORY)
        try:
            receive_entries(receiver, descriptor, MEGABYTES)
        except WireRefusedError:
            unread = bytearray()
            while chunk := receiver.recv(65536):
                unread += chunk
            return bytes(unread)
        finally:
            os.close(descriptor)
            writer.join()
    return None


def entries_in(deck: Path) -> int:
    return sum(1 for _ in deck.rglob("*"))


REFUSED_ROWS = [
    "absolute path", "//", "trailing /", ".", "..", "NUL", "256-byte component",
    "1025-byte path", "depth 33", "unknown entry tag", "unsorted", "duplicate",
    "orphan", "re-entered", "FILE over the budget", "more than 100000 entries",
    "END mismatch", "top-level node_modules",
]  # fmt: skip


def test_the_table_holds_every_refused_row_and_the_three_other_endings(
    uploads: ModuleType,
) -> None:
    refused = {
        name
        for name, row in uploads.UPLOAD_CLASSES.items()
        if row.ending is uploads.Ending.REFUSED
    }

    assert refused == set(REFUSED_ROWS)
    assert set(uploads.UPLOAD_CLASSES) - refused == {
        "dripped past U",
        "hang-up",
        "half-close",
    }


@pytest.mark.parametrize("name", REFUSED_ROWS)
def test_a_refused_rows_prefix_is_accepted_with_its_end(
    uploads: ModuleType, name: str, tmp_path: Path
) -> None:
    row = uploads.UPLOAD_CLASSES[name]

    unread = receive(row.prefix + row.prefix_end, tmp_path / "deck")

    assert unread is None
    assert entries_in(tmp_path / "deck") == row.prefix_entries


@pytest.mark.parametrize("name", REFUSED_ROWS)
def test_a_refused_row_is_refused_at_its_defect_with_its_declared_tail(
    uploads: ModuleType, name: str, tmp_path: Path
) -> None:
    row = uploads.UPLOAD_CLASSES[name]

    unread = receive(row.prefix + row.defect + SENTINEL, tmp_path / "deck")

    assert unread is not None
    assert len(unread) == row.unread_tail + len(SENTINEL)
    assert unread.endswith(SENTINEL)
    assert entries_in(tmp_path / "deck") == row.prefix_entries


@pytest.mark.parametrize("name", REFUSED_ROWS)
def test_a_refused_row_ends_its_life_through_the_wire_exit(
    uploads: ModuleType, name: str
) -> None:
    assert uploads.UPLOAD_CLASSES[name].life_end is LifeEnd.WIRE_EXIT


def test_the_drip_is_valid_bytes_so_only_the_upload_time_ends_its_life(
    uploads: ModuleType, tmp_path: Path
) -> None:
    row = uploads.UPLOAD_CLASSES["dripped past U"]

    unread = receive(
        row.prefix + row.defect + uploads.end_record(row.prefix_entries + 1),
        tmp_path / "deck",
    )

    assert unread is None
    assert row.life_end is LifeEnd.UPLOAD_EXPIRED
    assert receive(row.prefix + row.prefix_end, tmp_path / "other") is None


@pytest.mark.parametrize("name", ["hang-up", "half-close"])
def test_a_prefix_cut_off_by_the_client_is_refused_with_only_its_entries_made(
    uploads: ModuleType, name: str, tmp_path: Path
) -> None:
    row = uploads.UPLOAD_CLASSES[name]

    unread = receive(row.prefix, tmp_path / "deck")

    assert unread == b""
    assert entries_in(tmp_path / "deck") == row.prefix_entries
