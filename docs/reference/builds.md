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

An instance builds every deck in a container of its own. The server asks the
daemon for a container per step: no network at all, every capability dropped,
no new privilege from a setuid program, no log kept by Docker (the failure tail
is read from the client's stderr), and none of this server's environment.
It sees two directories of the builds volume, the deck's own tree read-only and
the directory that run writes its talk and PDF into, neither named after
anything a deck's author chose; everything else it writes is the container's
own and goes with it. It runs as the image's unprivileged user. A deck that
reads a file it does not carry, or opens a connection, fails the build with the
toolchain's own words on the deck page, and the talk that stood keeps standing.
The container's own root is left writable because Slidev's PDF export writes
its dependency cache inside the toolchain it runs from; that filesystem is the
image's and lives as long as the one build.

An instance refuses to start, naming which, without the image and the volume it
builds in, on a daemon older than Docker Engine 26 (API 1.45; the client is
pinned to it besides, so a daemon swapped under a running instance refuses the
call rather than mounting more than it was asked for), on a daemon that does
not carry the build image, or on a machine that will not hold a container's
own filesystem to a size unless the operator says to do without that bound
([runbook](../runbooks/run-builds-without-a-disk-bound.md)). Building on this
machine instead is `PRESENTATOR_BUILD_RUNNER=host`, for a development run; it
costs the sandbox altogether.

## Bounds

| Bound | Setting | What happens at it |
| --- | --- | --- |
| Time, per step | `PRESENTATOR_BUILD_TIMEOUT_SECONDS` (`300`) | The step is killed and its container taken down; the page says the build was given up on |
| Memory | `PRESENTATOR_BUILD_MEMORY` (`4g`) | The kernel ends the build, and the toolchain's own words say so |
| Processes | 512, a constant | The container may spawn no more |
| Its own filesystem | `PRESENTATOR_BUILD_DISK` (`8g`) | Writing past it fails inside the build |
| The talk it leaves | `PRESENTATOR_BUILD_OUTPUT_MEGABYTES` (`300`) | The step still running is stopped there, in this server's own words on the page, and what it wrote is taken away |
| The files that talk holds | 100 000, a constant | The same, in the same words |
| What it writes into a file it has unlinked | Nothing counts it | No scan can see such a file, so the step's time and the memory bound are what limit it, and the container's exit frees it |

The talk is added up while the build runs, not only when it ends, because a
machine is filled long before a build is over; a step that has written past
either bound does not get to finish. Nothing in a run's own directory is
followed while that happens: a link a build leaves counts as the name it is,
never as what it points at, and adding up stops at the first entry this server
cannot read rather than passing over it.

Which storage drivers take the disk bound is not guessed from a name: at every
start the server makes and starts one container of its own, bounded like a
build's, running nothing, and takes it down. With `PRESENTATOR_BUILD_DISK=none`
the log says at every start that nothing but a step's time bounds what a build
writes beside its talk.
