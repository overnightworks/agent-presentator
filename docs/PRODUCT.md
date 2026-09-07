# Product status

Audience: humans and agents deciding what agent-presentator currently is. This
index owns the implementation status. Where the vision or a decision record
describes something this file does not list, that thing is not built.

## What exists today

An instance signs a person in, lists the decks a configured Git source
carries while keeping that list current by itself, gives each deck a page,
builds the deck a push changed, and delivers that talk and its PDF from the
page. Nothing is deployed, so no phase of [VISION.md](VISION.md) is
reached. M0 is tracked on
[#8](https://github.com/overnightworks/agent-presentator/issues/8); first start
and login landed as
[#22](https://github.com/overnightworks/agent-presentator/issues/22), the deck
list as
[#26](https://github.com/overnightworks/agent-presentator/issues/26), the deck
page and the serving boundary as
[#33](https://github.com/overnightworks/agent-presentator/issues/33).

A decision record is a technical choice, not a claim that its slice exists.

## Sections

This index gains a section per subject once that subject has landed behavior to
report.

### Signing in

The first start of an empty instance creates the admin at `/setup`, and that
page is gone as soon as an account exists; there is no other way to an account
yet. `/login` opens a session that SQLite holds, carried by a signed cookie
that expires after twelve idle hours and slides forward on every request.
Wrong name, wrong password, and too many attempts answer with one sentence.
Logging out is a POST that deletes the session row. Every other address, known
or not, answers a redirect to the login until someone is signed in, and no
answer may be replayed from the browser cache. A form another site submitted is
refused: first start and login are answered without a cookie, so `SameSite`
does not cover them, and until `webauth` brings its `CsrfPolicy`
([ADR 0003](decisions/0003-libraries-for-models-and-auth.md)) the lobby checks
that a submitted form came from this instance.

The whole identity implementation is a bridge until `webauth` is tagged
([ADR 0003](decisions/0003-libraries-for-models-and-auth.md),
[ADR 0011](decisions/0011-instance-users.md)): songmaker
[#835](https://github.com/overnightworks/songmaker/issues/835) brings the
stores and [#833](https://github.com/overnightworks/songmaker/issues/833) the
user management, and it is deleted with them. How an instance is started is
[OPERATIONS.md](OPERATIONS.md).

### Listing decks

One Git source is configured with `PRESENTATOR_SOURCE_URL`, an optional
`PRESENTATOR_SOURCE_REF`, and an optional `PRESENTATOR_SOURCE_CREDENTIAL`
naming the environment variable that carries a read-only secret — the
configuration holds the reference, never the value. `gitmirror` keeps a bare
mirror of that repository under `PRESENTATOR_MIRRORS` by driving `git` as a
subprocess ([ADR 0010](decisions/0010-git-sources-mirror.md)); nothing is ever
checked out, and the tree is read at one commit. The pull runs with a minimal
environment that cannot prompt, and inside
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS`, so an unreachable source costs one poll
that bound and no more.

A deck pushed to the source appears without anyone asking for it: the server
polls every `PRESENTATOR_SOURCE_POLL_SECONDS` on a task beside the routes, which
never runs two pulls at once and starts the next tick after the bound ends a
slow one. Where a host can call back, `POST /hooks/<source>` with the source's
own secret does the same at once; it reads no payload, so every git host and a
`post-receive` hook are the same caller, and a wrong secret, a missing secret,
an unknown source, and any other path there answer alike. That address exists
only while a secret of at least 32 characters arms it, and only that one POST
is open — everything else under it stays behind the login, as does every
address on an instance that carries no hook secret. A flood of calls collapses
into the one refresh that runs at a time. Opening the deck list only reads the
database. A source that cannot be read is logged and says nothing about what it
carries, so every deck stays listed; a folder whose manifest cannot be read is
logged too and counts as a folder without a title, so the deck row it belongs to
stands as it was. A folder counts as a deck when it carries both `deck.toml` and
`slides.md`; its folder name is the slug and therefore its address, so changing
`title` in the manifest changes no link. A folder the source no longer carries
leaves the list: the refresh marks its deck as removed instead of deleting
anything, and a folder pushed again under the same name loses that mark and is
the deck it was, at the same address and with the same owner. Only a refresh
reconciles, so a page view never does. The most recently changed deck stands
first, and a deck belongs to the account that owns the source it came from — for
a configured source, the admin that first start created. While no deck exists,
the list says so and names the Git address instead of showing an empty table;
there is no upload, no editing, and no way to add a source in the lobby
([ADR 0005](decisions/0005-deck-folder-and-slidev.md)).

Sources and their secrets in the store, the fetch log, and build states are open
on
[#8](https://github.com/overnightworks/agent-presentator/issues/8).

### A deck's page, and the talk behind it

`/deck/<folder>` shows the deck's title, its folder, the address it was
mirrored from, the short commit the talk it delivers was built from, and how
long ago that build ran, so before speaking a person sees whether their push is
in what will be on the screen. Until a build has switched anything over, the
commit shown is the one the source last carried under that folder.

Where a deck's built talk stands, which file its PDF is handed over as, which
commit both were built from, and when, are four columns on the deck's row that
one statement writes together: a reader can find the talk of one commit beside
the PDF and the build time of that same commit, never a mixture. Taking the
deck in from Git again moves none of them, so a new push never unpresents the
talk that already works and never takes away the PDF that already downloads.
A deck no build has switched over yet says so and offers no view rather than a
dead link.

While a build is pointed at, the projector view is `/deck/<folder>/` and the
presenter view `/deck/<folder>/presenter/`, both delivered out of that
directory as static files, with the slide in the address so a closed window
comes back to the same slide. Both stand behind the same session as every other
address of the instance ([ADR 0007](decisions/0007-browser-client-behind-tunnel.md)):
a signed-out request answers the login redirect, never a file and never a hint
that a folder exists. The address only chooses a row; the directory comes from
that row, so no part of a request becomes part of a path, and a path that would
leave the build directory — through `..`, through percent-encoded separators, or
through a symlink out of it — is refused. Every signed-in person may hold every
deck; a deck still belongs to the account that owns its source, and
per-person visibility is not ruled for this phase.

`/deck/<folder>/pdf` hands the file that row names over as `application/pdf`,
to be saved under the deck's folder name, and the page offers that download
only while the column is set. An address with nothing behind it — no file put
yet, or one that is gone from disk — answers the lobby's own not-found page
rather than a traceback, and a signed-out request answers the login like every
other address. The saved name is derived from the folder name alone, and a slug
that is no folder's own name — carrying a separator, a quote, or a line break —
hands over nothing at all, so nothing an address carries reaches a response
header.

### Building a deck

A refresh takes the source in and then builds every deck whose commit is not
the commit its talk was built from, so a push builds the deck it changed and
leaves the other talks alone. Builds run inside that refresh, which runs one at
a time beside the routes, so they follow one another and no page view waits for
one.

A build writes the deck's tree at its commit out of the bare mirror into a
temporary working directory — nothing is ever checked out into the mirror —
and runs the Slidev toolchain over it from `PRESENTATOR_TOOLCHAIN` as a
subprocess: `slidev build` against the address the talk is delivered under,
then `slidev export` for the PDF. Both write into a directory of that run's own
under `PRESENTATOR_BUILDS`, which nothing points at while it is being written.
Each step runs inside `PRESENTATOR_BUILD_TIMEOUT_SECONDS` and with an
environment holding nothing but `PATH` and `HOME`, so neither a source's
read-only secret nor this instance's key is in reach of what a deck's build
runs.

Only when both artefacts exist, and only after they are resolved and found to
stand under the builds root, does one statement switch the four columns over.
A build that failed, one that ran past its bound, one whose toolchain is not on
the machine, and one whose result stands anywhere else write no pointer at all:
the talk that already stands keeps standing and keeps its build time. What a
build that did not finish left behind is taken away again, and the directory a
deck delivers from is never removed, so a request that read the previous
pointer still finds a directory. Cleaning up the builds that were pointed at is
open on [#8](https://github.com/overnightworks/agent-presentator/issues/8).

**A deck is code, and it is not sandboxed yet.** The Vue components a deck
carries execute on this host during the build, with this process's rights over
the filesystem and the network. Bounded today are the environment the child is
given, the time it may take, and where its result may stand; the container with
no network and nothing of the server mounted
([ADR 0005](decisions/0005-deck-folder-and-slidev.md), line 14a) is open on
[#8](https://github.com/overnightworks/agent-presentator/issues/8). Until it
lands, a deck source is as trusted as the machine.

Which state a build is in — building, failed, and the error text on the deck's
page — is open on
[#8](https://github.com/overnightworks/agent-presentator/issues/8) too; today a
deck either delivers a talk or says it delivers none.
