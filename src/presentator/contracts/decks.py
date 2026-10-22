"""What a deck and its source are, everywhere in this product (ADR 0005)."""

import posixpath
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from ipaddress import IPv6Address
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote, urlparse

MANIFEST_FILE: Final = "deck.toml"
SLIDES_FILE: Final = "slides.md"
# The names one run of the build owns: the deck's own tree as the toolchain
# reads it, everything that run writes, and the talk and the PDF inside that.
DECK_DIRECTORY: Final = "deck"
OUTPUT_DIRECTORY: Final = "out"
TALK_DIRECTORY: Final = "talk"
PDF_FILE: Final = "deck.pdf"
# The directory the toolchain writes into the deck it reads.
TOOLCHAIN_SCRATCH_DIRECTORY: Final = "node_modules"
DECK_PATH: Final = "/deck"
_FILE_SCHEME: Final = "file"
# What a failed build may say on a page: the end of what its toolchain printed,
# where the reason stands. A deck's own build can print without limit, and a
# page is read by a person shortly before they speak.
FAILURE_TEXT_LIMIT: Final = 2000
# A page shows the end of what a step printed, so only that much of it is ever
# held: four bytes is the longest one character can be, so decoding and
# stripping still leave the full limit.
FAILURE_TAIL_BYTES: Final = 4 * FAILURE_TEXT_LIMIT
# A folder's name is one path element: it stays one word of a header value and
# never reaches past the directory it is joined under.
_NAMES_NO_FOLDER: Final = frozenset({"", ".", ".."})
_NEVER_IN_A_FOLDER_NAME: Final = frozenset('/\\"')


class StepKind(StrEnum):
    """The Slidev commands of one build, in the order they are run."""

    BUILD = "build"
    EXPORT = "export"


def bounded_failure(said: str) -> str:
    """The last of what a build said, short enough for a page to carry."""
    return said[-FAILURE_TEXT_LIMIT:]


def failure_text(said: bytes) -> str:
    """What a step printed, decoded and bounded for a page."""
    return bounded_failure(said.decode(errors="replace").strip())


def is_a_plain_folder_name(candidate: str) -> bool:
    """Whether the candidate is a folder's own name and nothing besides."""
    return (
        candidate not in _NAMES_NO_FOLDER
        and candidate.isprintable()
        and not _NEVER_IN_A_FOLDER_NAME.intersection(candidate)
    )


def talk_address(slug: str) -> str:
    """Where a deck's built talk stands, and what it is built against.

    A build writes its own asset links against this address, so the string the
    build is given and the address the lobby delivers it at are the one value.
    """
    return f"{DECK_PATH}/{slug}/"


# The Source board draws three runs, and that page is the first reader of the
# log, so the table keeps the same three: older rows have no caller.
RECENT_SOURCE_RUNS: Final = 3


class AccessKind(StrEnum):
    """How a source is read, derived from its URL's scheme and nothing else.

    HTTPS is a token; SSH and the scp form are a deploy key; a `file://`
    address or a bare absolute path is this box's own mount, and carries no
    secret at all. The radio on the form has to match this, and a mismatch is
    refused rather than stored as a fourth kind.
    """

    HTTPS = "https-token"
    SSH = "ssh-deploy-key"
    FILE = "local-folder"


# `file://` names no host but the machine that opens it; anything else in the
# authority is a different machine's address wearing a local scheme, and git
# itself ignores whatever stands there rather than refusing it, so this
# refuses it instead of inheriting that silent ambiguity.
_LOCAL_AUTHORITIES: Final = frozenset({"", "localhost"})


def local_mount_path_of(url: str) -> PurePosixPath | None:
    """The address's path, normalized, when it names a file-kind source.

    A file-kind address is either the `file://` scheme, whose authority must
    be empty or `localhost`, or a bare absolute path. A `file://` path is
    percent-decoded first, the way git itself decodes one before it opens it
    — a bare path is not, because git never decodes one either. `..` is
    collapsed lexically after that, so a caller judging whether the result
    still stands under the mount sees the path the address actually reaches
    rather than one a `..` component, encoded or not, could hide behind.
    This is still a lexical answer: the one place that asks the real
    filesystem — the only place a symlink or a mount that has since changed
    can be caught — is the adapter that resolves it against the mount at
    every use.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme == _FILE_SCHEME:
        if parsed.netloc not in _LOCAL_AUTHORITIES:
            return None
        raw = unquote(parsed.path)
    elif not parsed.scheme and url.startswith("/"):
        raw = url
    else:
        return None
    return PurePosixPath(posixpath.normpath(raw))


# Only these spellings of a remote address are an access. Every other
# spelling, however git or curl would read it, could name a different host than
# the one a person sees, so none of them may carry a secret. The authority is
# checked character by character, because curl, git and ssh each end it at a
# different place. The path of an ssh address holds no bracket, because git
# reads an `@[` anywhere in the address as the start of the host ssh contacts;
# an https path is only ever sent to the one host the authority names.
_HOST: Final = r"(?:[A-Za-z0-9.-]+|\[(?P<ipv6>[0-9A-Fa-f:.]+)\])"
_PORT: Final = r"(?::[0-9]+)?"
_HTTPS_PATH: Final = r"[^\s\\]*"
_SCP_PATH: Final = r"[^\s\\\[\]]*"
_SSH_URL_PATH: Final = r"[^\s\\\[\]%?#]*"
_URL_USER: Final = r"[A-Za-z0-9._~!$&'()*+,;=:-]"
_HTTPS_URL: Final = re.compile(
    rf"https://(?:(?:{_URL_USER}|%[0-9A-Fa-f]{{2}})+@)?{_HOST}{_PORT}/{_HTTPS_PATH}"
)
_SSH_URL: Final = re.compile(rf"ssh://(?:{_URL_USER}+@)?{_HOST}{_PORT}/{_SSH_URL_PATH}")
_SCP_ADDRESS: Final = re.compile(rf"[A-Za-z0-9._~-]+@{_HOST}:{_SCP_PATH}")


def _names_a_host(address: re.Match[str] | None) -> bool:
    """Whether the match is an address whose bracketed host, if any, is IPv6.

    git strips the brackets from `[name]` and ssh then resolves `name`, so a
    bracket that does not hold an IPv6 literal names a host the person cannot
    read off the address.
    """
    if address is None:
        return False
    literal = address.group("ipv6")
    if literal is None:
        return True
    try:
        IPv6Address(literal)
    except ValueError:
        return False
    return True


def _remote_access_kind_of(url: str) -> AccessKind | None:
    if _names_a_host(_HTTPS_URL.fullmatch(url)):
        return AccessKind.HTTPS
    if _names_a_host(_SSH_URL.fullmatch(url)) or _names_a_host(
        _SCP_ADDRESS.fullmatch(url)
    ):
        return AccessKind.SSH
    return None


def access_kind_of(url: str) -> AccessKind | None:
    """The access the URL's scheme names, or nothing when it names none.

    HTTPS is a token; SSH and the scp form (`git@host:path`) are a deploy
    key; `file://` or a bare absolute path is this box's own mount. Anything
    else is not an access this product has, and that includes a remote
    address whose host could be read two ways and any address holding a
    control character.
    """
    if any(unicodedata.category(character) == "Cc" for character in url):
        return None
    if (kind := _remote_access_kind_of(url)) is not None:
        return kind
    return AccessKind.FILE if local_mount_path_of(url) is not None else None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceWrite:
    """What the store is given to insert: the row, the access value, and the hash."""

    name: str
    url: str
    ref: str
    owner_id: str
    access_secret: str
    hook_secret_hash: bytes
    # The public half of a deploy key, in clear beside the encrypted private
    # half; nothing while the source carries an HTTPS token or no secret.
    public_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """A git repository decks are mirrored from, holding no secret value."""

    id: str
    name: str
    url: str
    ref: str
    owner_id: str
    public_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeployKeyDraft:
    """An admin's own unbound SSH keypair: what the Add form shows and binds.

    Server-held, keyed by the admin who owns it (ADR 0010's credential
    vocabulary) rather than by session, so a reload still shows the key the
    operator may already have pasted into the host. It is never a source.
    """

    id: str
    owner_id: str
    public_key: str
    private_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckFolder:
    """A folder of a mirrored source as it stands at one commit."""

    name: str
    file_names: frozenset[str]
    title: str | None
    changed_at: datetime
    commit: str


class SourceRunOutcome(StrEnum):
    """Whether a poll of a source found its newest commit or could not.

    Closed to these two. The third word a board may show — "never fetched" —
    is not a value here: a source nobody has polled yet has no run at all, so
    that case is told apart by the absence of a `SourceRun`, not by a member
    of this enum.
    """

    SUCCESS = "success"
    FAILURE = "failure"


class SourceRunFailure(StrEnum):
    """Why a poll failed, in this product's own words rather than gitmirror's.

    A separate enum from `gitmirror.model.ConnectionState` keeps that
    package's exact wording from reaching a board by accident; the adapter,
    which already speaks both vocabularies, owns the translation between them.
    """

    CREDENTIAL_UNRESOLVABLE = "credential-unresolvable"
    REFUSED = "refused"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePoll:
    """What one poll of a source found: its folders and the commit reached.

    A source nobody could read carries no folders and no commit, only the
    reason; a source that answered carries both, even when it carries no
    folder at all.
    """

    folders: tuple[DeckFolder, ...] | None
    commit: str | None
    failure: SourceRunFailure | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionCheckResult:
    """What a bounded probe of an unsaved source found, without writing anything.

    No failure and a commit is reachable; a failure and no commit is not, and
    carries git's own sanitised first line only for `SourceRunFailure.FAILED`
    — the one reason with no fixed sentence of its own. The fingerprint is
    the proof the form later needs to create what this probe found, and
    travels only when the state is reachable.
    """

    failure: SourceRunFailure | None
    commit: str | None
    detail: str | None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRun:
    """One poll of a source, kept so a board can show that it was ever tried.

    The commit and the reason are each the other's absence: a run that reached
    the source carries the commit it found and no reason, one that did not
    carries the reason and no commit.
    """

    source_id: str
    at: datetime
    outcome: SourceRunOutcome
    commit: str | None
    reason: SourceRunFailure | None


# The page a built talk opens on, in the talk's own root: the build checks the
# file the lobby serves, so both name it through this one value.
TALK_PAGE: Final = "index.html"


@dataclass(frozen=True, slots=True, kw_only=True)
class Artefacts:
    """What one run of the build left on disk: a talk to serve, and its PDF."""

    directory: Path
    pdf: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildFailure:
    """That a build produced nothing, and what its toolchain said about it.

    The words are the toolchain's own, bounded by `bounded_failure`, or nothing
    where none reached the server — a run given up on, or one that printed
    nothing at all.
    """

    text: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Build:
    """The talk a deck delivers, the commit it was built from, and when.

    The four travel together because they switch together: a page that named a
    build time of one commit beside the directory of another would lie about
    what is on the screen.
    """

    directory: Path
    pdf: Path
    commit: str
    built_at: datetime


class BuildOutcome(StrEnum):
    """How the build a deck last started ended, while it is not its talk yet."""

    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildAttempt:
    """The build a deck last started, for as long as it did not become its talk.

    A running attempt is one nothing has reported back about yet and carries no
    failure; a failed one carries what its toolchain said, or nothing where no
    words reached the server at all. The successful switch clears the record,
    so a deck carries an attempt only while there is something to say about it.
    """

    commit: str
    started_at: datetime
    outcome: BuildOutcome
    failure: str | None


class DeckState(StrEnum):
    """What the list and the deck page say a deck is, in one word.

    It is read off the talk that stands and the attempt beside it, never
    stored: two records that can each be written on their own would otherwise
    disagree about one deck.
    """

    READY = "ready"
    BUILDING = "building"
    FAILED = "failed"
    NEVER_BUILT = "never-built"


@dataclass(frozen=True, slots=True, kw_only=True)
class Deck:
    """A talk. The folder name is the identity; the title only shows."""

    slug: str
    title: str
    changed_at: datetime
    owner_id: str
    source_id: str
    commit: str
    build: Build | None
    attempt: BuildAttempt | None

    def is_owned_by(self, viewer: str) -> bool:
        """Whether the account owns this talk; only its owner may see it."""
        return self.owner_id == viewer


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedDeck:
    """One row of the deck list, with the state and the age the row shows."""

    slug: str
    title: str
    state: DeckState
    age: timedelta


class SourceState(StrEnum):
    """What the sources list says a source is, read off its newest run.

    No run is never-fetched, a successful run is reachable, and the reason a
    credential the instance could not resolve never becomes a word on the
    board: it folds into error. A refused login and a host that answered with
    something else each keep their own word instead, because a wrong token, a
    dead host, and a host that answered but not with the repository are three
    different things an operator needs told apart.
    """

    REACHABLE = "reachable"
    ERROR = "error"
    REFUSED = "refused"
    FAILED = "failed"
    NEVER_FETCHED = "never-fetched"


@dataclass(frozen=True, slots=True, kw_only=True)
class ListedSource:
    """One row of the sources list, with the state and the age the row shows."""

    name: str
    url: str
    access: AccessKind | None
    state: SourceState
    age: timedelta | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ShownSourceRun:
    """One recent poll as the source page shows it: age, commit, or why not."""

    outcome: SourceRunOutcome
    age: timedelta
    commit: str | None
    reason: SourceRunFailure | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDeck:
    """A deck this source carries, named the way the page's chips name it."""

    slug: str
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePage:
    """What one source's page says: is it working, and what comes from it."""

    name: str
    url: str
    access: AccessKind | None
    state: SourceState
    age: timedelta | None
    public_key: str | None
    runs: tuple[ShownSourceRun, ...]
    decks: tuple[SourceDeck, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRemoval:
    """What removing a source takes with it: its name, its decks, its runs.

    Read before anything is deleted, so a confirm can name this and a person
    can still say no; the same value stands for what the list then reports
    as gone, because nothing changes between the two.
    """

    name: str
    deck_count: int
    run_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ShownAttempt:
    """The build a deck page reports on: its commit, its age, and what broke."""

    commit: str
    ago: timedelta
    failure: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckPage:
    """What a deck's own page says about it before anyone speaks."""

    slug: str
    title: str
    source: str | None
    commit: str
    built_ago: timedelta | None
    state: DeckState
    attempt: ShownAttempt | None
    # The themes this instance's toolchain carries (ADR 0014), or nothing
    # while its project cannot be read: an empty set is never shown as if it
    # were the real one (R3).
    themes: tuple[str, ...] | None
