"""The web's client of the build worker: one worker life per step, over its socket."""

import errno
import logging
import os
import shutil
import socket
import stat
import time
from collections.abc import Buffer, Callable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from presentator.adapters.build_wire import (
    MAXIMUM_ENTRIES,
    SOCKET_DIRECTORY_MODE,
    SOCKET_MODE,
    TOOLCHAIN_SCRATCH_NAME,
    BuiltOutcome,
    FailedOutcome,
    RefusalLimit,
    RefusedOutcome,
    StepHeader,
    TreeRefusedError,
    WireRefusedError,
    check_step_tree,
    check_upload,
    peer_uid,
    read_empty_end,
    read_outcome,
    read_status,
    receive_entries,
    send_hello,
    send_step,
    send_tree,
)
from presentator.adapters.builds import (
    TOO_MANY_FOR_A_PAGE,
    TOO_MUCH_FOR_A_PAGE,
    BuildPlace,
    BuildStep,
)
from presentator.contracts.decks import (
    DECK_DIRECTORY,
    OUTPUT_DIRECTORY,
    PDF_FILE,
    TALK_DIRECTORY,
    BuildFailure,
    StepKind,
)

CONNECT_WINDOW_SECONDS: Final = 70.0
FIRST_RETRY_SECONDS: Final = 0.25
LONGEST_RETRY_SECONDS: Final = 4.0
REPLY_MARGIN_SECONDS: Final = 5.0
STAGED_PREFIX: Final = "staged-"
STAGING_MODE: Final = 0o700

OUTPUT_SHAPE_FOR_A_PAGE: Final = (
    "The talk this build wrote cannot be taken: it holds a link, a special or"
    " unreadable file, a path of more than 32 parts (folders and the file) or"
    " 1024 bytes, or a file this step may not write."
)
DECK_SIZE_FOR_A_PAGE: Final = (
    "This deck is too large to build: a deck may hold at most {megabytes} MB and"
    " at most {entries} files and folders together, not counting its top-level"
    " node_modules."
)
DECK_PATHS_FOR_A_PAGE: Final = (
    "This deck cannot be built: it holds a path of more than 32 parts (folders"
    " and the file) or 1024 bytes, or a link, a special or an unreadable file."
)
_FAULTED: Final = "build %s of deck %s failed: %s"
_ANSWERING: Final = "the build worker did not answer: %s (%s)"
_NOT_REMOVED: Final = "the staging %s of a build reply could not be removed: %s"

_log = logging.getLogger(__name__)


class Fault(StrEnum):
    """The class of a fault of one exchange, named in its log line."""

    NOT_THE_WORKER = "what answered is not the worker"
    DEADLINE = "a deadline passed"
    CONNECTION = "the connection broke"
    REPLY_GRAMMAR = "the reply broke the wire grammar"
    STEP_CONTRACT = "the reply broke the step contract or a deck rule"
    LOCAL = "this machine refused a read or write"


class SocketFault(StrEnum):
    """What makes the directory, the socket or the peer not the worker's."""

    DIRECTORY_OWNER = "directory owner"
    DIRECTORY_MODE = "directory mode"
    DIRECTORY_GROUP = "directory group"
    SOCKET_TYPE = "socket type"
    SOCKET_OWNER = "socket owner"
    SOCKET_MODE = "socket mode"
    PEER = "peer"


class NotTheWorkerError(Exception):
    """The socket directory, the socket or its peer is not what the worker makes."""

    def __init__(self, cause: SocketFault) -> None:
        """Carry what was wrong."""
        super().__init__(cause)
        self.cause = cause


class _NotYetError(Exception):
    """The worker is not there yet; the attempt is retried within the window."""


def what_is_wrong(
    directory: os.stat_result,
    socket_status: os.stat_result,
    peer_uid: int | None,
    *,
    uid: int,
    gid: int,
) -> SocketFault | None:
    """The first way the directory, the socket or the peer is not the worker's.

    An unconnected socket has no peer yet; its peer is judged once it has one.
    """
    checks = (
        (SocketFault.DIRECTORY_OWNER, directory.st_uid == uid),
        (
            SocketFault.DIRECTORY_MODE,
            directory.st_mode == stat.S_IFDIR | SOCKET_DIRECTORY_MODE,
        ),
        (SocketFault.DIRECTORY_GROUP, directory.st_gid == gid),
        (SocketFault.SOCKET_TYPE, stat.S_ISSOCK(socket_status.st_mode)),
        (SocketFault.SOCKET_OWNER, socket_status.st_uid == uid),
        (
            SocketFault.SOCKET_MODE,
            stat.S_IMODE(socket_status.st_mode) == SOCKET_MODE,
        ),
        (SocketFault.PEER, peer_uid in {None, uid}),
    )
    return next((cause for cause, holds in checks if not holds), None)


def _cause_of(error: BaseException) -> str:
    """The errno name of the first fault behind an error, else its class name."""
    while error.__cause__ is not None:
        error = error.__cause__
    if isinstance(error, OSError) and error.errno is not None:
        return errno.errorcode[error.errno]
    return type(error).__name__


def fault_of(error: Exception) -> Fault:
    """The class of fault an exchange's exception is."""
    if isinstance(error, NotTheWorkerError):
        return Fault.NOT_THE_WORKER
    if isinstance(error, WireRefusedError):
        return Fault.REPLY_GRAMMAR
    if isinstance(error, TreeRefusedError):
        return Fault.STEP_CONTRACT
    if isinstance(error, TimeoutError):
        return Fault.DEADLINE
    if isinstance(error, ConnectionError):
        return Fault.CONNECTION
    return Fault.LOCAL


class _DeadlineSocket(socket.socket):
    """A socket whose calls together wait only until one deadline.

    A timeout set once bounds each call, not their sum, so a reply dripped a
    byte at a time would outlive it; every call here is armed with what is left.
    """

    deadline = float("inf")

    def until(self, deadline: float) -> None:
        """Bound every later call by this monotonic time."""
        self.deadline = deadline

    def _armed(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.settimeout(remaining)

    def connect(self, address: str | Buffer | tuple[object, ...]) -> None:
        """Connect within the deadline."""
        self._armed()
        super().connect(address)

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        """Receive within the deadline."""
        self._armed()
        return super().recv(bufsize, flags)

    def sendall(self, data: Buffer, flags: int = 0) -> None:
        """Send everything within the deadline."""
        self._armed()
        super().sendall(data, flags)


def _begun_to_answer(connection: socket.socket) -> None:
    """Peek the first reply byte; a close before it means the worker is not ready."""
    if not connection.recv(1, socket.MSG_PEEK):
        raise _NotYetError


def _nothing_follows(connection: socket.socket) -> None:
    if connection.recv(1):
        raise WireRefusedError


def _refusal_text(outcome: RefusedOutcome, megabytes: int) -> str:
    """The page text of a refused reply; a written count off its limit is a fault."""
    if (outcome.written is not None) != (outcome.limit is RefusalLimit.MEGABYTES):
        raise WireRefusedError
    if outcome.limit is RefusalLimit.MEGABYTES:
        return TOO_MUCH_FOR_A_PAGE.format(written=outcome.written, allowed=megabytes)
    if outcome.limit is RefusalLimit.ENTRIES:
        return TOO_MANY_FOR_A_PAGE.format(allowed=MAXIMUM_ENTRIES)
    return OUTPUT_SHAPE_FOR_A_PAGE


def _admitted(staged: Path, place: BuildPlace, kind: StepKind) -> None:
    """Move the step's one admitted entry into out/, which must not hold it yet."""
    name = TALK_DIRECTORY if kind is StepKind.BUILD else PDF_FILE
    target = place.here / OUTPUT_DIRECTORY / name
    if os.path.lexists(target):
        raise FileExistsError(target)
    (staged / name).rename(target)


def _staged_reply(
    connection: socket.socket, staged: Path, step: BuildStep, megabytes: int
) -> None:
    """Receive a built reply into the staging, hold it to the contract, admit it."""
    descriptor = os.open(staged, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        receive_entries(connection, descriptor, megabytes)
        _nothing_follows(connection)
        check_step_tree(descriptor, step.name, megabytes)
    finally:
        os.close(descriptor)
    _admitted(staged, step.place, step.name)


def _failure_text(
    outcome: FailedOutcome | RefusedOutcome, megabytes: int
) -> str | None:
    if isinstance(outcome, RefusedOutcome):
        return _refusal_text(outcome, megabytes)
    return outcome.text


def _answer(
    connection: socket.socket, staged: Path, step: BuildStep, megabytes: int
) -> BuildFailure | None:
    outcome = read_outcome(connection)
    if isinstance(outcome, BuiltOutcome):
        _staged_reply(connection, staged, step, megabytes)
        return None
    text = _failure_text(outcome, megabytes)
    read_empty_end(connection)
    _nothing_follows(connection)
    return BuildFailure(text=text)


def _failure_of(error: Exception, step: BuildStep) -> BuildFailure:
    _log.warning(_FAULTED, step.name, step.slug, fault_of(error))
    return BuildFailure(text=None)


def _cleared(staged: Path, remove_staging: Callable[[Path], None]) -> None:
    try:
        remove_staging(staged)
    except OSError as error:
        _log.warning(_NOT_REMOVED, staged.name, error)


def exchange_with_worker(
    connection: socket.socket,
    *,
    step: BuildStep,
    deck: int,
    megabytes: int,
    remove_staging: Callable[[Path], None],
) -> BuildFailure | None:
    """Upload the deck and take the worker's reply for a step it has answered.

    Every fault of the exchange comes back as a BuildFailure with one log line,
    and the staging is gone afterwards; nothing but the step's one admitted
    entry reaches out/.
    """
    staged = step.place.here / f"{STAGED_PREFIX}{step.name}"
    try:
        staged.mkdir(mode=STAGING_MODE)
        try:
            send_tree(connection, deck, omitting=TOOLCHAIN_SCRATCH_NAME)
            return _answer(connection, staged, step, megabytes)
        finally:
            _cleared(staged, remove_staging)
    except (OSError, WireRefusedError, TreeRefusedError) as error:
        return _failure_of(error, step)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerToolchain:
    """Runs a step by asking the build worker, which owns the sealed container.

    One worker life serves one step: the web sends the step, uploads the deck
    and takes the output back into out/. Before the worker's STATUS everything
    waits only until the connect window ends; afterwards only until the
    worker's own last deadline plus a margin has passed.
    """

    socket_path: Path
    megabytes: int
    reply_bound_seconds: float
    expected_uid: int
    expected_gid: int
    connect_window_seconds: float = CONNECT_WINDOW_SECONDS
    first_retry_seconds: float = FIRST_RETRY_SECONDS
    longest_retry_seconds: float = LONGEST_RETRY_SECONDS
    reply_margin_seconds: float = REPLY_MARGIN_SECONDS
    remove_staging: Callable[[Path], None] = shutil.rmtree

    def nothing_of_it_runs(self, place: BuildPlace) -> bool:
        """Nothing does: the worker's steps run in its own container."""
        del place
        return True

    def is_answering(self) -> bool:
        """Whether the worker answers a HELLO within the connect window."""
        window_ends = time.monotonic() + self.connect_window_seconds
        try:
            with closing(
                self._opened(send_hello, window_ends, at_start=True)
            ) as connection:
                read_status(connection)
        except (OSError, WireRefusedError, NotTheWorkerError) as error:
            _log.warning(_ANSWERING, fault_of(error), _cause_of(error))
            return False
        return True

    def ran(self, step: BuildStep) -> BuildFailure | None:
        """Send the deck to the worker for that step and admit what it built."""
        try:
            deck = os.open(
                step.place.here / DECK_DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError as error:
            return _failure_of(error, step)
        try:
            return self._refused_deck(deck, step) or self._through_worker(deck, step)
        finally:
            os.close(deck)

    def _refused_deck(self, deck: int, step: BuildStep) -> BuildFailure | None:
        try:
            check_upload(deck, self.megabytes)
        except TreeRefusedError as refusal:
            if refusal.outcome.limit is RefusalLimit.SHAPE:
                return BuildFailure(text=DECK_PATHS_FOR_A_PAGE)
            return BuildFailure(
                text=DECK_SIZE_FOR_A_PAGE.format(
                    megabytes=self.megabytes, entries=MAXIMUM_ENTRIES
                )
            )
        except OSError as error:
            return _failure_of(error, step)
        return None

    def _through_worker(self, deck: int, step: BuildStep) -> BuildFailure | None:
        window_ends = time.monotonic() + self.connect_window_seconds
        header = StepHeader(step=step.name, slug=step.slug)
        try:
            with closing(
                self._opened(lambda sent: send_step(sent, header), window_ends)
            ) as connection:
                read_status(connection)
                connection.until(
                    window_ends + self.reply_bound_seconds + self.reply_margin_seconds
                )
                return exchange_with_worker(
                    connection,
                    step=step,
                    deck=deck,
                    megabytes=self.megabytes,
                    remove_staging=self.remove_staging,
                )
        except (OSError, WireRefusedError, NotTheWorkerError) as error:
            return _failure_of(error, step)

    def _opened(
        self,
        first_message: Callable[[socket.socket], None],
        window_ends: float,
        *,
        at_start: bool = False,
    ) -> _DeadlineSocket:
        """A connection to the worker that has begun to answer its first message.

        Retried within the window while the worker is not there yet; a socket
        that is not the worker's is never retried. At start only, a socket the
        web may not yet search for is "not yet" too: a fresh socket volume is
        the worker's own until its fix-up gives it to the web's group.
        """
        wait = self.first_retry_seconds
        while True:
            try:
                return self._attempt(first_message, window_ends, at_start=at_start)
            except _NotYetError as not_yet:
                remaining = window_ends - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError from not_yet
                time.sleep(min(wait, remaining))
                wait = min(2 * wait, self.longest_retry_seconds)

    def _attempt(
        self,
        first_message: Callable[[socket.socket], None],
        window_ends: float,
        *,
        at_start: bool,
    ) -> _DeadlineSocket:
        connection = _DeadlineSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.until(window_ends)
        try:
            socket_status = self._socket_status(at_start=at_start)
            directory = self.socket_path.parent.lstat()
            self._judged(directory, socket_status, None)
            connection.connect(str(self.socket_path))
            self._judged(directory, socket_status, peer_uid(connection))
            first_message(connection)
            _begun_to_answer(connection)
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
        ) as error:
            connection.close()
            raise _NotYetError from error
        except BaseException:
            connection.close()
            raise
        return connection

    def _socket_status(self, *, at_start: bool) -> os.stat_result:
        """The socket's own status; read before the directory's.

        A socket the web can read lies in a directory the worker's fix-up has
        already given the web's group.
        """
        try:
            return self.socket_path.lstat()
        except PermissionError as error:
            if not at_start:
                raise
            owner = self.socket_path.parent.lstat().st_uid
            if owner != self.expected_uid:
                raise NotTheWorkerError(SocketFault.DIRECTORY_OWNER) from error
            raise _NotYetError from error

    def _judged(
        self,
        directory: os.stat_result,
        socket_status: os.stat_result,
        peer_uid: int | None,
    ) -> None:
        cause = what_is_wrong(
            directory,
            socket_status,
            peer_uid,
            uid=self.expected_uid,
            gid=self.expected_gid,
        )
        if cause is not None:
            raise NotTheWorkerError(cause)
