# Product status

Audience: humans and agents deciding what agent-presentator currently is. This
index owns the implementation status. Where the vision or a decision record
describes something this file does not list, that thing is not built.

## What exists today

An instance signs a person in, lists the decks a configured Git source
carries while keeping that list current by itself, gives each deck a page,
and delivers a deck's built talk from that page. Nothing builds that talk
yet, and nothing is deployed, so no phase of [VISION.md](VISION.md) is
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
database. A source that cannot be read, and a folder whose manifest cannot be
read, are logged and leave the rest of the list standing. A folder counts as a
deck when it carries both `deck.toml` and `slides.md`; its folder name is the
slug and therefore its address, so changing `title` in the manifest changes no
link. The most recently changed deck stands first, and a deck belongs to the
account that owns the source it came from — for a configured source, the admin
that first start created. While no deck exists, the list says so and names the
Git address instead of showing an empty table; there is no upload, no editing,
and no way to add a source in the lobby
([ADR 0005](decisions/0005-deck-folder-and-slidev.md)).

Sources and their secrets in the store, the fetch log, reconciling a folder
deleted in Git, and build states are open on
[#8](https://github.com/overnightworks/agent-presentator/issues/8).

### A deck's page, and the talk behind it

`/deck/<folder>` shows the deck's title, its folder, the address it was
mirrored from, and the short commit that folder was read at, so before speaking
a person sees which state the delivered talk stands at. The build time joins
them with the slice that builds.

Where a deck's built talk stands, and which file its PDF is handed over as, are
two columns on the deck's row. Only putting one of them moves it, and taking the
deck in from Git again leaves both standing, so a new push never unpresents the
talk that already works and never takes away the PDF that already downloads.
Nothing writes those columns yet — the build itself, its states, and the export
are open on [#8](https://github.com/overnightworks/agent-presentator/issues/8) —
so a deck page says no talk has been built from it yet and offers no view rather
than a dead link.

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
