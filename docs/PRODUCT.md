# Product status

Audience: humans and agents deciding what agent-presentator currently is. This
index owns the implementation status. Where the vision or a decision record
describes something this file does not list, that thing is not built.

## What exists today

An instance signs a person in and lists the decks a configured Git source
carries, keeping that list current by itself. Nothing is built, presented, or
deployed yet, so no phase of [VISION.md](VISION.md) is reached. M0 is tracked on
[#8](https://github.com/overnightworks/agent-presentator/issues/8); first start
and login landed as
[#22](https://github.com/overnightworks/agent-presentator/issues/22), the deck
list as
[#26](https://github.com/overnightworks/agent-presentator/issues/26).

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
and an unknown source answer alike. A flood of calls collapses into the one
refresh that runs at a time. Opening the deck list only reads the database. A
source that cannot be read, and a folder whose manifest cannot be read, are
logged and leave the rest of the list standing. A folder counts as a deck when
it carries both `deck.toml` and `slides.md`; its folder name is the slug and
therefore its address, so changing `title` in the manifest changes no link. The
most recently changed deck stands first, and a deck belongs to the account that
owns the source it came from — for a configured source, the admin
that first start created. While no deck exists, the list says so and names the
Git address instead of showing an empty table; there is no upload, no editing,
and no way to add a source in the lobby
([ADR 0005](decisions/0005-deck-folder-and-slidev.md)).

A deck's page, its build, and its PDF do not exist yet, so a row links to an
address that answers nothing. Sources and their secrets in the store, the fetch
log, reconciling a folder deleted in Git, and build states are open on
[#8](https://github.com/overnightworks/agent-presentator/issues/8).
