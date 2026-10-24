# Builds reference

Audience: an engineer or operator looking up how a deck is built, what one
build may take, and when an instance refuses to start. Why a build is sandboxed
is [trust boundaries](../explanation/trust-boundaries.md).

## When a deck builds

A refresh takes the source in and then builds every deck whose commit neither
its talk was built from nor a build has already been attempted at. A push
builds the deck it changed, leaves other talks alone, and a deck that cannot
build costs one build rather than one per refresh until it is pushed again.
Builds run inside that refresh, which runs one at a time beside the routes, so
they follow one another and no page view waits for one.

The deck's row carries the build it last started: the commit, when it began,
whether it is running or failed, and what it said when it failed. The refresh
writes that record before the toolchain starts; the successful switch clears
it in the same statement that moves the talk. An attempt still saying it runs
when the next refresh reads it belongs to a process that is gone (a server
restarted mid-build) and is counted failed once it is older than
`PRESENTATOR_BUILD_TIMEOUT_SECONDS`.

## What a build does

It writes the deck's tree at its commit out of the bare mirror into a directory
of that run's own under `PRESENTATOR_BUILDS` (nothing is checked out into the
mirror) and runs the Slidev toolchain over it: `slidev build` against the
address the talk is delivered under, then `slidev export` for the PDF. Both
write into that run's directory, which nothing points at while it is being
written. Each step runs inside `PRESENTATOR_BUILD_TIMEOUT_SECONDS`.

Only when both artefacts exist, and only after they are resolved and found to
stand under the builds root, does one statement switch the four delivery
columns over ([decks](decks.md)). A build that failed, ran past its bound,
whose toolchain is not on the machine, or whose result stands anywhere else
writes no pointer at all: the talk that already stands keeps standing with its
build time, and the attempt beside it says why. What the toolchain printed
travels back as the reason, cut to its last two thousand characters, because
it is what the deck's page shows; a run given up on, one whose tree this host
could not read, and one that printed nothing hand back no words, and the page
says that in its own sentence. What a build that did not finish left behind is
taken away again, and the directory a deck delivers from is never removed, so
a request that read the previous pointer still finds a directory. A build once
pointed at is not cleaned up automatically, so `PRESENTATOR_BUILDS` grows with
every push; only Remove on a source's page takes the built directories of its
decks off disk.

## The toolchain

`PRESENTATOR_TOOLCHAIN` names a Node project that carries Slidev, installed
with `pnpm install --frozen-lockfile` under the Node version its `.nvmrc`
names. A deck may name a theme or an addon only from what that project already
carries; widening the set is a change to `frontend/package.json`, not a
decision a deck's push gets to make
([ADR 0014](../decisions/0014-toolchain-owns-build-dependencies.md)). That
`package.json` is the set's one owner, and a deck's page names it. The PDF
export drives a browser: `playwright-chromium` is one of that project's
dependencies.

The container image carries the toolchain, the browser in `/opt/ms-playwright`
and corepack's package managers in `/opt/corepack`, root's and readable by
every user. It also carries two users for a build worker, made before the
runtime user: `presentator-build` (10001, home `/work/home`) and
`presentator-wire` (10002, home `/nonexistent`), both without a login shell,
and its build refuses a runtime user of either uid. Slidev's export keeps its
dependency cache in `/work/vite`, a world-writable directory the image links to
from beside `@slidev/cli`. Without a toolchain the instance still runs: every
build fails and the failure is in the server log.

## The sandbox

The instance of `compose.yaml` builds every deck through the build worker (next
section): the web sends each step over a socket, and the worker runs it sealed,
in a container of its own, as an unprivileged user with no network, every
capability dropped and none of the server's environment. The web holds no
Docker socket and no docker group. A deck that reads a file it does not carry,
or opens a connection, fails the build with the toolchain's own words on the
deck page, and the talk that stood keeps standing.

The container runner is the code default until it leaves: with
`PRESENTATOR_BUILD_RUNNER` unset, a server asks a daemon for a container per
step and refuses to start without `PRESENTATOR_BUILD_IMAGE` and
`PRESENTATOR_BUILD_VOLUME`. `compose.yaml` sets neither. Building on this
machine instead is `PRESENTATOR_BUILD_RUNNER=host`, for a development run; it
costs the sandbox altogether.

## Bounds

| Bound | Setting | What happens at it |
| --- | --- | --- |
| Time, per step | `PRESENTATOR_BUILD_TIMEOUT_SECONDS` (`300`) | The step is killed and the page says the build was given up on |
| Memory | `mem_limit` of the worker, `4g`, swap included | The kernel ends the worker's life, and the next step runs in a fresh one |
| Processes | `pids_limit` of the worker, 512 | The container may spawn no more |
| Its own filesystem | the worker's 2 GiB `/work` | Writing past it fails inside the build |
| The talk it leaves | `PRESENTATOR_BUILD_OUTPUT_MEGABYTES` (`300`) | Both ends check it: the worker refuses the reply, and the web shows the page text below |
| The files that talk holds | 100 000, a constant | The same, in the same words |
| A deck sent to be built | the same megabytes and 100 000 entries, not counting its top-level `node_modules` | The web refuses it before it connects to the worker |

A connection to the worker is not bounded by the build's bound alone: before the
worker answers the web waits at most the connect window (70 seconds, retrying
with a growing pause), and afterwards at most the step's bound plus a margin.

### Page texts

Each text stands once, in the constant named beside it, and the deck page shows
it whole:

| Constant | Text |
| --- | --- |
| `TOO_MUCH_FOR_A_PAGE` (`adapters/builds.py`) | The talk this build wrote is {written} MB; a build may leave at most {allowed} MB. |
| `TOO_MANY_FOR_A_PAGE` (`adapters/builds.py`) | The talk this build wrote holds more than {allowed} files; a build may leave fewer. |
| `OUTPUT_SHAPE_FOR_A_PAGE` (`adapters/build_worker.py`) | The talk this build wrote cannot be taken: it holds a link, a special or unreadable file, a path of more than 32 parts (folders and the file) or 1024 bytes, or a file this step may not write. |
| `DECK_SIZE_FOR_A_PAGE` (`adapters/build_worker.py`) | This deck is too large to build: a deck may hold at most {megabytes} MB and at most {entries} files and folders together, not counting its top-level node_modules. |
| `DECK_PATHS_FOR_A_PAGE` (`adapters/build_worker.py`) | This deck cannot be built: it holds a path of more than 32 parts (folders and the file) or 1024 bytes, or a link, a special or an unreadable file. |

## The build worker

`compose.yaml` declares `build-worker`, the sealed service of
[ADR 0017](../decisions/0017-a-build-worker-runs-each-step-sealed.md). The
`presentator` service runs with `PRESENTATOR_BUILD_RUNNER=worker`, mounts the
`build-socket` volume at `/run/presentator-build` and depends on the worker
having started; the worker's socket is `worker.sock` in that volume. The web
refuses to start until the worker answers, and retries across a restarting
worker within the connect window. The worker needs the image the `presentator`
service builds and never pulls one.

The worker reads `PRESENTATOR_BUILD_TIMEOUT_SECONDS` and
`PRESENTATOR_BUILD_OUTPUT_MEGABYTES` from the project's environment by name; a
variable the project leaves unset keeps its default. `presentator-worker
--check` runs only the self-check and exits `0` in the sealed service, and `2`
naming the first failing predicate anywhere else.

A connection is served by a wire child, and one life serves one step: the
client sends HELLO or STEP and reads STATUS. A HELLO ends there: it only
asks whether the worker is ready. A STEP goes on to upload the deck and read
the OUTCOME and the output. The codec's client half is in
`presentator.adapters.build_wire`.
