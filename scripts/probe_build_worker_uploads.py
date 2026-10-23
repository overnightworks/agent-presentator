"""The probe's crafted uploads: each row is a valid prefix and one defect.

Every byte derives from the codec's constants. A row says where the controller
must refuse it (`unread_tail`) and how the client leaves it (`ending`), so the
discriminator in tests/adapters/test_probe_upload_classes.py can show that the
bytes a probe sends are refused at the byte their class names.
"""

from dataclasses import dataclass
from enum import StrEnum

from presentator.adapters.build_wire import (
    END_COUNTS,
    LENGTH,
    MAXIMUM_DEPTH,
    MAXIMUM_ENTRIES,
    MAXIMUM_PATH_BYTES,
    SIZE,
    TOOLCHAIN_SCRATCH_NAME,
    RecordTag,
)
from presentator.contracts.decks import MAXIMUM_NAME_BYTES
from presentator.host.worker_serving import UPLOAD_SECONDS, LifeEnd


class Ending(StrEnum):
    """How the client leaves an upload."""

    REFUSED = "refused"
    DRIPPED = "dripped"
    HUNG_UP = "hung up"
    HALF_CLOSED = "half-closed"


@dataclass(frozen=True)
class UploadClass:
    """A valid prefix, its END counts, and the one defect that follows it."""

    prefix: bytes
    prefix_entries: int
    defect: bytes
    unread_tail: int = 0
    ending: Ending = Ending.REFUSED
    life_end: LifeEnd = LifeEnd.WIRE_EXIT

    @property
    def prefix_end(self) -> bytes:
        """The END that closes the prefix alone (the prefix holds no file bytes)."""
        return end_record(self.prefix_entries)


def end_record(entries: int) -> bytes:
    """An END record counting this many entries and no file bytes."""
    return bytes([RecordTag.END]) + END_COUNTS.pack(entries, 0)


def _path_record(tag: int, path: bytes) -> bytes:
    return bytes([tag]) + LENGTH.pack(len(path)) + path


def directory(path: bytes) -> bytes:
    """A DIR record."""
    return _path_record(RecordTag.DIR, path)


def _empty_file(path: bytes) -> bytes:
    return _path_record(RecordTag.FILE, path) + SIZE.pack(0)


def _after_the_tag(defect: bytes) -> int:
    return len(defect) - 1


def _after_a_directory(
    defect: bytes,
    unread_tail: int = 0,
    ending: Ending = Ending.REFUSED,
    life_end: LifeEnd = LifeEnd.WIRE_EXIT,
) -> UploadClass:
    return UploadClass(directory(b"a"), 1, defect, unread_tail, ending, life_end)


def _directory_class(defect_path: bytes, prefix: bytes = b"a") -> UploadClass:
    return UploadClass(directory(prefix), 1, directory(defect_path))


def _deepest_chain() -> bytes:
    return b"".join(
        directory(b"/".join([b"a"] * depth)) for depth in range(1, MAXIMUM_DEPTH + 1)
    )


def _unknown_tag_record() -> bytes:
    return _path_record(max(RecordTag) + 1, b"b")


def _entries_at_the_maximum() -> bytes:
    width = len(str(MAXIMUM_ENTRIES))
    return b"".join(
        _empty_file(b"%0*d" % (width, number)) for number in range(MAXIMUM_ENTRIES)
    )


def _drip_record() -> bytes:
    """A valid DIR below `a` that one byte a second cannot finish before U + 5 s."""
    filler = int(UPLOAD_SECONDS) + 5
    return directory(b"a/" + b"b" * filler)


ABSOLUTE_PATH_CLASS = "absolute path"
# The first three rows also meet a second check (orphan for `/b` and `b//c`,
# rising name for `a/`). They are refused by path_components, which runs from
# _read_path before _enter_parent; a reorder in the codec would keep them
# refused by another rule while the discriminator stays green.
_DEFECTS_AFTER_A_DIRECTORY = {
    ABSOLUTE_PATH_CLASS: b"/b",
    "//": b"b//c",
    "trailing /": b"a/",
    ".": b"a/.",
    "..": b"a/..",
    "NUL": b"b\0c",
    "256-byte component": b"x" * (MAXIMUM_NAME_BYTES + 1),
    "duplicate": b"a",
    "orphan": b"b/c",
    "top-level node_modules": TOOLCHAIN_SCRATCH_NAME,
}
_OVERLONG_PATH = LENGTH.pack(MAXIMUM_PATH_BYTES + 1) + b"a" * (MAXIMUM_PATH_BYTES + 1)
_ONE_TOO_MANY = _empty_file(b"%0*d" % (len(str(MAXIMUM_ENTRIES)), MAXIMUM_ENTRIES))
_OVER_THE_BUDGET = _path_record(RecordTag.FILE, b"b") + SIZE.pack(
    2 ** (8 * SIZE.size) - 1
)
_UNKNOWN_TAG = _unknown_tag_record()

UPLOAD_CLASSES = {
    **{
        name: _directory_class(path)
        for name, path in _DEFECTS_AFTER_A_DIRECTORY.items()
    },
    "1025-byte path": _after_a_directory(
        bytes([RecordTag.DIR]) + _OVERLONG_PATH, unread_tail=MAXIMUM_PATH_BYTES + 1
    ),
    "depth 33": UploadClass(
        _deepest_chain(),
        MAXIMUM_DEPTH,
        directory(b"/".join([b"a"] * (MAXIMUM_DEPTH + 1))),
    ),
    "unknown entry tag": _after_a_directory(
        _UNKNOWN_TAG, unread_tail=_after_the_tag(_UNKNOWN_TAG)
    ),
    "unsorted": _directory_class(b"a", prefix=b"b"),
    "re-entered": UploadClass(directory(b"a") + directory(b"b"), 2, directory(b"a/c")),
    "FILE over the budget": _after_a_directory(_OVER_THE_BUDGET),
    "more than 100000 entries": UploadClass(
        _entries_at_the_maximum(),
        MAXIMUM_ENTRIES,
        _ONE_TOO_MANY,
        unread_tail=_after_the_tag(_ONE_TOO_MANY),
    ),
    "END mismatch": _after_a_directory(end_record(2)),
    "dripped past U": _after_a_directory(
        _drip_record(), ending=Ending.DRIPPED, life_end=LifeEnd.UPLOAD_EXPIRED
    ),
    "hang-up": _after_a_directory(b"", ending=Ending.HUNG_UP),
    "half-close": _after_a_directory(b"", ending=Ending.HALF_CLOSED),
}
