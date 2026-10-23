"""The web's client of the build worker, against a fake worker on a real socket."""

import errno
import logging
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from presentator.adapters.build_wire import (
    MAXIMUM_ENTRIES,
    MEGABYTE,
    SOCKET_DIRECTORY_MODE,
    SOCKET_MODE,
    SOCKET_NAME,
    FailedOutcome,
    RefusalLimit,
    RefusedOutcome,
    StepHeader,
    WireRefusedError,
    read_request,
    receive_entries,
    send_status,
)
from presentator.adapters.build_worker import (
    DECK_PATHS_FOR_A_PAGE,
    DECK_SIZE_FOR_A_PAGE,
    OUTPUT_SHAPE_FOR_A_PAGE,
    STAGED_PREFIX,
    SocketFault,
    WorkerToolchain,
    exchange_with_worker,
    what_is_wrong,
)
from presentator.adapters.builds import (
    TOO_MANY_FOR_A_PAGE,
    TOO_MUCH_FOR_A_PAGE,
    BuildPlace,
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
from presentator.host import worker
from tests import worker_support as support
from tests.worker_support import OWN_GID, OWN_UID

MEGABYTES = 1
WINDOW = 0.6
PROMPTLY = 10
WINDOW_TOLERANCE = 0.1
AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores file modes, so 000 refuses nothing"
)
WITHOUT_A_REASON = BuildFailure(text=None)
INDEX = b"<html>"
PDF = b"%PDF"
BUILT_TALK = support.OUTCOME_BUILT + support.talk_entries(INDEX)
BUILT_PDF = (
    support.OUTCOME_BUILT
    + support.file_entry(PDF_FILE.encode(), PDF)
    + support.end_entry(1, len(PDF))
)
BUILT_BY_STEP = {StepKind.BUILD: BUILT_TALK, StepKind.EXPORT: BUILT_PDF}


def refused_reply(limit: RefusalLimit, written: int | None = None) -> bytes:
    header = RefusedOutcome(limit=limit, written=written).model_dump_json().encode()
    return support.framed(header, b"PBW\x01\x05") + support.END


def failed_reply(text: str | None) -> bytes:
    header = FailedOutcome(text=text).model_dump_json().encode()
    return support.framed(header, b"PBW\x01\x05") + support.END


@dataclass
class Seen:
    """What the fake worker saw of its clients."""

    steps: list[StepHeader] = field(default_factory=list[StepHeader])
    uploads: list[Path] = field(default_factory=list[Path])


type Script = Callable[[socket.socket, Seen, Path], None]


def answering(reply: Callable[[socket.socket, StepHeader], None]) -> Script:
    """A worker that answers a HELLO, and a STEP by taking the upload and replying."""

    def serve(connection: socket.socket, seen: Seen, uploads: Path) -> None:
        request = read_request(connection)
        send_status(connection)
        if not isinstance(request, StepHeader):
            return
        seen.steps.append(request)
        upload = uploads / str(len(seen.steps))
        upload.mkdir()
        seen.uploads.append(upload)
        descriptor = os.open(upload, os.O_RDONLY | os.O_DIRECTORY)
        try:
            receive_entries(connection, descriptor, MEGABYTES)
        finally:
            os.close(descriptor)
        reply(connection, request)

    return serve


def nothing_to_reply(_connection: socket.socket, _step: StepHeader) -> None:
    return None


def replying(by_step: dict[StepKind, bytes]) -> Script:
    def reply(connection: socket.socket, step: StepHeader) -> None:
        connection.sendall(by_step[step.step])

    return answering(reply)


def closing_first(count: int, then: Script, *, after_the_request: bool) -> Script:
    """A worker that closes its first connections, as a busy or restarting one does."""
    remaining = [count]

    def serve(connection: socket.socket, seen: Seen, uploads: Path) -> None:
        if remaining[0]:
            remaining[0] -= 1
            if after_the_request:
                read_request(connection)
            return
        then(connection, seen, uploads)

    return serve


@contextmanager
def fake_worker(volume: Path, script: Script, accepts: int = 4) -> Generator[Seen]:
    """A thread serving the script on the worker's real socket in the volume."""
    listener = support.listening_with(volume, accepts)
    seen = Seen()
    uploads = volume.parent / "uploads"
    uploads.mkdir()

    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except (support.StopServingError, OSError):
                return
            with connection, suppress(OSError, WireRefusedError):
                if not stopped.is_set():
                    script(connection, seen, uploads)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield seen
    finally:
        stopped.set()
        listener.accepts_left = 1
        with socket.socket(socket.AF_UNIX) as waker:
            waker.connect(str(volume / SOCKET_NAME))
        thread.join()
        listener.close()


@pytest.fixture
def volume(tmp_path: Path) -> Generator[Path]:
    """The socket volume; the worker's umask, set by its bind, ends with the test."""
    before = os.umask(0)
    os.umask(before)
    (tmp_path / "volume").mkdir()
    yield tmp_path / "volume"
    os.umask(before)


def toolchain(volume: Path, **overrides: object) -> WorkerToolchain:
    base = WorkerToolchain(
        socket_path=volume / SOCKET_NAME,
        megabytes=MEGABYTES,
        reply_bound_seconds=2.0,
        expected_uid=OWN_UID,
        expected_gid=OWN_GID,
        connect_window_seconds=WINDOW,
        first_retry_seconds=0.02,
        longest_retry_seconds=0.1,
        reply_margin_seconds=0.2,
    )
    return replace(base, **overrides)


def a_step(root: Path, kind: StepKind = StepKind.BUILD) -> BuildStep:
    place = BuildPlace(root=root / "builds", run=Path("run"))
    (place.here / DECK_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (place.here / DECK_DIRECTORY / "slides.md").write_text("# hello")
    (place.here / OUTPUT_DIRECTORY).mkdir(exist_ok=True)
    return BuildStep(
        place=place, slug="my-deck", name=kind, arguments=(), watched=lambda: None
    )


def tree_of(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


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


def staging_of(step: BuildStep) -> Path:
    return step.place.here / f"{STAGED_PREFIX}{step.name}"


def stat_result(
    mode: int = stat.S_IFDIR | SOCKET_DIRECTORY_MODE, uid: int = 0, gid: int = 7
) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


GOOD_DIRECTORY = stat_result()
GOOD_SOCKET = stat_result(stat.S_IFSOCK | SOCKET_MODE, gid=0)


@pytest.mark.parametrize(
    ("directory", "socket_status", "peer_uid", "cause"),
    [
        pytest.param(stat_result(uid=5), GOOD_SOCKET, 0, SocketFault.DIRECTORY_OWNER),
        pytest.param(
            stat_result(stat.S_IFDIR | 0o755),
            GOOD_SOCKET,
            0,
            SocketFault.DIRECTORY_MODE,
        ),
        pytest.param(
            stat_result(stat.S_IFLNK | SOCKET_DIRECTORY_MODE),
            GOOD_SOCKET,
            0,
            SocketFault.DIRECTORY_MODE,
        ),
        pytest.param(stat_result(gid=8), GOOD_SOCKET, 0, SocketFault.DIRECTORY_GROUP),
        pytest.param(
            GOOD_DIRECTORY,
            stat_result(stat.S_IFREG | SOCKET_MODE, gid=0),
            0,
            SocketFault.SOCKET_TYPE,
        ),
        pytest.param(
            GOOD_DIRECTORY,
            stat_result(stat.S_IFSOCK | SOCKET_MODE, uid=5),
            0,
            SocketFault.SOCKET_OWNER,
        ),
        pytest.param(
            GOOD_DIRECTORY,
            stat_result(stat.S_IFSOCK | 0o666),
            0,
            SocketFault.SOCKET_MODE,
        ),
        pytest.param(GOOD_DIRECTORY, GOOD_SOCKET, 5, SocketFault.PEER),
        pytest.param(GOOD_DIRECTORY, GOOD_SOCKET, 0, None),
        pytest.param(GOOD_DIRECTORY, GOOD_SOCKET, None, None),
    ],
)
def test_the_socket_check_names_the_first_thing_that_is_not_the_workers(
    directory: os.stat_result,
    socket_status: os.stat_result,
    peer_uid: int | None,
    cause: SocketFault | None,
) -> None:
    """Cell B3."""
    assert what_is_wrong(directory, socket_status, peer_uid, uid=0, gid=7) is cause


def test_a_worker_that_is_not_there_is_retried_until_the_window_ends(
    volume: Path,
) -> None:
    """Cells B1 and B14: start without a worker is refused after W."""
    started = time.monotonic()

    answering_at_start = toolchain(volume / "absent").is_answering()

    assert not answering_at_start
    assert WINDOW <= time.monotonic() - started < WINDOW + 0.5


@pytest.mark.parametrize("after_the_request", [False, True])
def test_a_worker_that_keeps_closing_is_retried_until_the_window_ends(
    volume: Path, *, after_the_request: bool
) -> None:
    """Cell B1: a close before STATUS is retried, and the failure comes at W."""
    keeps_closing = closing_first(
        50, answering(nothing_to_reply), after_the_request=after_the_request
    )
    started = time.monotonic()

    with fake_worker(volume, keeps_closing, accepts=50):
        answering_at_start = toolchain(volume).is_answering()

    assert not answering_at_start
    assert WINDOW <= time.monotonic() - started < WINDOW + 0.5


def test_a_socket_not_yet_listening_is_retried_until_the_window_ends(
    volume: Path,
) -> None:
    """Cell B1."""
    worker.prepare_socket_directory(volume, OWN_UID, OWN_GID)
    worker.bind_socket(volume, OWN_UID, OWN_GID)
    started = time.monotonic()

    assert not toolchain(volume).is_answering()

    assert WINDOW <= time.monotonic() - started < WINDOW + 0.5


def test_a_worker_answering_a_hello_is_answering(volume: Path) -> None:
    """Cell B14: a worker that answers the HELLO at start is answering."""
    with fake_worker(volume, answering(nothing_to_reply)):
        assert toolchain(volume).is_answering()


def test_a_peer_that_does_not_speak_the_wire_is_not_answering(volume: Path) -> None:
    """Cell B5 at start: a reply that breaks the grammar is no answer."""

    def babbling(connection: socket.socket, _seen: Seen, _uploads: Path) -> None:
        read_request(connection)
        connection.sendall(b"nonsense")

    with fake_worker(volume, babbling):
        assert not toolchain(volume).is_answering()


@pytest.mark.parametrize("closes", [1, 3])
@pytest.mark.parametrize("after_the_request", [False, True])
def test_a_worker_closing_then_answering_serves_start_build_and_export(
    volume: Path, tmp_path: Path, closes: int, *, after_the_request: bool
) -> None:
    """Cell B12 (I): the next build succeeds once the worker's life has ended."""
    script = closing_first(
        closes, replying(BUILT_BY_STEP), after_the_request=after_the_request
    )
    with fake_worker(volume, script, accepts=closes + 3):
        client = toolchain(volume, connect_window_seconds=5.0)

        started = client.is_answering()
        built = client.ran(a_step(tmp_path))
        exported = client.ran(a_step(tmp_path, StepKind.EXPORT))

    out = tmp_path / "builds" / "run" / OUTPUT_DIRECTORY
    assert (started, built, exported) == (True, None, None)
    assert (out / TALK_DIRECTORY / "index.html").read_bytes() == INDEX
    assert (out / PDF_FILE).read_bytes() == PDF


def test_a_directory_that_is_not_the_workers_is_refused_without_a_byte_or_a_retry(
    volume: Path, tmp_path: Path
) -> None:
    """Cell B3 (I)."""
    started = time.monotonic()
    with fake_worker(volume, replying(BUILT_BY_STEP)) as seen:
        refused = toolchain(
            volume, expected_uid=OWN_UID + 1, connect_window_seconds=30.0
        ).ran(a_step(tmp_path))

    assert refused == WITHOUT_A_REASON
    assert not seen.steps
    assert time.monotonic() - started < PROMPTLY


@AS_ROOT
def test_a_socket_directory_that_cannot_be_searched_is_refused_without_a_retry(
    tmp_path: Path,
) -> None:
    """Cell B3: EACCES."""
    (tmp_path / "outer" / "run").mkdir(parents=True)
    (tmp_path / "outer").chmod(0)
    started = time.monotonic()
    try:
        answering_at_start = toolchain(
            tmp_path / "outer" / "run", connect_window_seconds=30.0
        ).is_answering()
    finally:
        (tmp_path / "outer").chmod(0o755)

    assert not answering_at_start
    assert time.monotonic() - started < PROMPTLY


def test_a_reply_dripped_past_the_last_deadline_fails_at_it(
    volume: Path, tmp_path: Path
) -> None:
    """Cell B4."""

    def dripping(connection: socket.socket, _step: StepHeader) -> None:
        for byte in BUILT_TALK:
            connection.sendall(bytes([byte]))
            time.sleep(0.02)

    client = toolchain(volume, reply_bound_seconds=0.3)
    started = time.monotonic()
    with fake_worker(volume, answering(dripping)):
        result = client.ran(a_step(tmp_path))

    assert result == WITHOUT_A_REASON
    ends_by = WINDOW + 0.3 + 0.2
    assert ends_by - 0.05 <= time.monotonic() - started < ends_by + 0.5


def test_a_status_later_than_the_window_fails_at_it_with_no_upload_byte_sent(
    volume: Path, tmp_path: Path
) -> None:
    """Cell B22: the STEP goes out well into W, and its STATUS comes after W."""
    seen_late: list[bytes] = []

    def slow_status(connection: socket.socket, _seen: Seen, _uploads: Path) -> None:
        read_request(connection)
        time.sleep(1.0)
        connection.settimeout(1)
        seen_late.append(connection.recv(1 << 16))

    after_three_closings = closing_first(3, slow_status, after_the_request=False)
    started = time.monotonic()
    with fake_worker(volume, after_three_closings):
        result = toolchain(volume).ran(a_step(tmp_path))
        elapsed = time.monotonic() - started

    assert result == WITHOUT_A_REASON
    assert abs(elapsed - WINDOW) < WINDOW_TOLERANCE
    assert seen_late == [b""]


def test_a_deck_with_node_modules_uploads_only_the_nested_one(
    volume: Path, tmp_path: Path
) -> None:
    """Cell B11."""
    step = a_step(tmp_path)
    deck = step.place.here / DECK_DIRECTORY
    (deck / "node_modules").mkdir()
    (deck / "node_modules" / "x").write_text("x")
    (deck / "a" / "node_modules").mkdir(parents=True)
    (deck / "a" / "node_modules" / "y").write_text("y")

    with fake_worker(volume, replying(BUILT_BY_STEP)) as seen:
        built = toolchain(volume).ran(step)

    assert built is None
    assert sorted(tree_of(seen.uploads[0])) == [
        "a",
        "a/node_modules",
        "a/node_modules/y",
        "slides.md",
    ]


def a_deck_over_the_megabytes(deck: Path) -> None:
    with (deck / "big").open("wb") as big:
        big.truncate(MEGABYTES * MEGABYTE)


def a_deck_over_the_entries(deck: Path) -> None:
    for number in range(MAXIMUM_ENTRIES):
        os.close(os.open(deck / f"{number:06d}", os.O_CREAT | os.O_WRONLY))


def a_deck_with_a_deep_path(deck: Path) -> None:
    deepest = deck
    for _ in range(33):
        deepest = deepest / "d"
        deepest.mkdir()


@pytest.mark.parametrize(
    ("make", "text"),
    [
        pytest.param(
            a_deck_over_the_megabytes,
            DECK_SIZE_FOR_A_PAGE.format(megabytes=MEGABYTES, entries=MAXIMUM_ENTRIES),
            id="megabytes",
        ),
        pytest.param(
            a_deck_over_the_entries,
            DECK_SIZE_FOR_A_PAGE.format(megabytes=MEGABYTES, entries=MAXIMUM_ENTRIES),
            id="entries",
        ),
        pytest.param(a_deck_with_a_deep_path, DECK_PATHS_FOR_A_PAGE, id="path rule"),
    ],
)
def test_a_deck_over_a_limit_or_breaking_a_rule_says_so_and_connects_to_nothing(
    volume: Path, tmp_path: Path, make: Callable[[Path], None], text: str
) -> None:
    """Cell B10: no socket exists, so a connect would have retried until W."""
    step = a_step(tmp_path)
    make(step.place.here / DECK_DIRECTORY)
    started = time.monotonic()

    result = toolchain(volume / "absent", connect_window_seconds=30.0).ran(step)

    assert result == BuildFailure(text=text)
    assert time.monotonic() - started < PROMPTLY


def test_a_deck_that_cannot_be_walked_fails_without_a_reason(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault table: a deck-walk OSError other than the shape class."""
    step = a_step(tmp_path)

    def failing_stat(*_arguments: object, **_options: object) -> os.stat_result:
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "stat", failing_stat)

    assert toolchain(volume / "absent").ran(step) == WITHOUT_A_REASON


def test_a_run_without_its_deck_fails_without_a_reason(
    volume: Path, tmp_path: Path
) -> None:
    """Sentence B4: a run with its deck gone fails as a BuildFailure, never a raise."""
    step = a_step(tmp_path)
    (step.place.here / DECK_DIRECTORY / "slides.md").unlink()
    (step.place.here / DECK_DIRECTORY).rmdir()

    assert toolchain(volume / "absent").ran(step) == WITHOUT_A_REASON


def test_a_deck_changing_after_the_survey_fails_without_a_reason(
    volume: Path, tmp_path: Path
) -> None:
    """Fault table: a deck that changes between the survey and the upload."""
    step = a_step(tmp_path)

    def changing_the_deck(
        connection: socket.socket, _seen: Seen, _uploads: Path
    ) -> None:
        read_request(connection)
        (step.place.here / DECK_DIRECTORY / "link").symlink_to("slides.md")
        send_status(connection)

    with fake_worker(volume, changing_the_deck):
        result = toolchain(volume).ran(step)

    assert result == WITHOUT_A_REASON
    assert not staging_of(step).exists()


def test_a_worker_closing_while_the_deck_is_uploaded_fails_without_a_reason(
    volume: Path, tmp_path: Path
) -> None:
    """Cell B5: EPIPE or ECONNRESET after STATUS is not retried."""
    step = a_step(tmp_path)
    (step.place.here / DECK_DIRECTORY / "big").write_bytes(b"x" * (MEGABYTE // 2))

    def closing_after_status(
        connection: socket.socket, _seen: Seen, _uploads: Path
    ) -> None:
        read_request(connection)
        send_status(connection)

    with fake_worker(volume, closing_after_status) as _:
        result = toolchain(volume).ran(step)

    assert result == WITHOUT_A_REASON
    assert not staging_of(step).exists()


def test_a_worker_closing_in_the_middle_of_its_reply_fails_without_a_reason(
    volume: Path, tmp_path: Path
) -> None:
    """Cell B5 (I): the worker closes in the middle of its reply."""
    step = a_step(tmp_path)

    def closing_mid_reply(connection: socket.socket, _step: StepHeader) -> None:
        connection.sendall(support.OUTCOME_BUILT)

    with fake_worker(volume, answering(closing_mid_reply)):
        result = toolchain(volume).ran(step)

    assert result == WITHOUT_A_REASON
    assert not staging_of(step).exists()


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


def test_the_worker_runs_nothing_that_could_write_in_the_run_directory(
    volume: Path, tmp_path: Path
) -> None:
    """Trace row: nothing_of_it_runs answers True."""
    assert toolchain(volume).nothing_of_it_runs(a_step(tmp_path).place)
