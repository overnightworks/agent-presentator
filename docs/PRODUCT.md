# Product status

Audience: humans and agents deciding what agent-presentator currently is. This
index owns the implementation status. Where the vision or a decision record
describes something this file does not list, that thing is not built.

## What exists today

An instance signs a person in, lists the decks a configured Git source
carries while keeping that list current by itself, gives each deck a page,
builds the deck a push changed, shows what state that build is in, delivers
that talk and its PDF from the page, and lets an admin and every person say how
the lobby looks and which language it speaks. M0 is tracked on
[#8](https://github.com/overnightworks/agent-presentator/issues/8); first start
and login landed as
[#22](https://github.com/overnightworks/agent-presentator/issues/22), the deck
list as
[#26](https://github.com/overnightworks/agent-presentator/issues/26), the deck
page and the serving boundary as
[#33](https://github.com/overnightworks/agent-presentator/issues/33), and
Settings, Account and the person menu as
[#32](https://github.com/overnightworks/agent-presentator/issues/32).

A decision record is a technical choice, not a claim that its slice exists.

## Sections

This index gains a section per subject once that subject has landed behavior to
report.

### Running an instance

One image carries the Slidev toolchain and the Chromium the PDF export drives,
and is what every deck's build runs in; a second, built on top of it, carries
the packaged server as well. One compose file builds both and starts the server
with the database, the mirrors and the built talks in named volumes: a `docker
compose down` and the next `up` find the accounts, the sources, the decks and
the talks that were built, and build nothing again. Nothing is reachable from
beyond this machine yet — the tunnel, the service that survives a reboot, and
the first login from the laptop are
[#67](https://github.com/overnightworks/agent-presentator/issues/67). How an
instance is started, upgraded and backed up, and what a fresh one must be
given, is [OPERATIONS.md](OPERATIONS.md).

### Signing in

The first start of an empty instance creates the admin at `/setup`, and that
page is gone as soon as an account exists; there is no other way to an account
yet. `/login` opens a session that SQLite holds, carried by a signed cookie
that expires after twelve idle hours and slides forward on every request.
`webauth` v0.3.1 judges credentials, counts the throttle, owns the cookie and
CSRF-origin mechanism, and formats the refusal for a missing session. The
host's `Identity` still decides which SQLite session names a signed-in person.
A signed-out browser navigation goes to `/login` with the requested path and
query in `next`; a JSON request receives 401. Every mutating route is
cross-origin protected by default; the sessionless `POST /sources/<name>/fetch`
is the only exemption. Wrong name, wrong password, and too many attempts
answer with one sentence — never a per-outcome status or message. Logging out
is a POST that deletes the session row. No answer may be replayed from the
browser cache.

The SQLite stores, the Argon2id hasher, and first start stay this repository's
([ADR 0003](decisions/0003-libraries-for-models-and-auth.md),
[ADR 0011](decisions/0011-instance-users.md)). User management, including
deactivation, belongs to [#61](https://github.com/overnightworks/agent-presentator/issues/61).
How an instance is started is [OPERATIONS.md](OPERATIONS.md).

### Settings, Account, and how the lobby looks

Every signed-in page carries the picture's header: the product name, the Decks
section, Settings for an admin, and one person control that opens Account, the
three theme rows, and Log out.

An admin opens Settings and sets the instance name, the default language, and
the default theme; a person without the admin role is refused there rather than
sent to the login. Settings is one area with a tab strip: General is those
instance defaults, and Sources lists each source with the state and age of its
newest run — never fetched, reachable, refused when the host turns the login
down, failed when the host answered with something else, or Error when a
credential could not be resolved, never the internal reason why — and a Fetch
now that refreshes that one source and returns to the list. An instance with
no source says what a source
is and what you need for it. Users is not a tab yet. An admin adds a source
there, and each source has a page of its own. Everybody opens Account
and overrides language and theme for themselves alone. Both resolve the same way — the person's own choice first,
the instance default behind it — and "follow system" writes no `data-theme`
attribute at all, so the browser decides
([ADR 0012](decisions/0012-themes-and-language.md)). The instance defaults live
in one SQLite row, a person's overrides in a row beside their account, and a
theme chosen in the person menu is that same Account preference.

English is the only catalog the repository ships. Every catalog file the
catalog directory holds is offered as a language, so a second language is a
file and not a change to code, and each catalog names itself. The instance name
is kept and shown back under Settings; nothing else reads it yet.

The lobby's colours come from the vendored
[`overnightworks/ui-console`](https://github.com/overnightworks/ui-console)
v0.2.0 pin; Pico leftover chrome lives in `lobby.css`. Settings and Account
still use this product's Pico markup; the package grammar is adopted when
Users lands
([#61](https://github.com/overnightworks/agent-presentator/issues/61)).

htmx 2.x is vendored beside Pico and carries the two writes a person makes
about themselves: the theme rows of the person menu and the Account
preferences post, and the page is painted again in what they chose.
jinja2-fragments ([ADR 0008](decisions/0008-lobby-server-rendered.md)) is not
installed, because no surface here has a partial to serve: both writes change
the whole document, down to the `<html>` element the theme sits on.

Account carries neither the password nor the sessions part of the picture;
each arrives with the slice that fills it.

### Listing decks

A source is a row: an id, a name and a URL that are each unique, the ref it
follows, and the account that owns it. An admin adds one under Settings ·
Sources with a name and Git URL. HTTPS needs a read-only token; SSH opens Add
with one account-owned ed25519 public key to register at the Git host. *Check
connection* probes the typed HTTPS token or owned SSH draft before anything is
stored, and *Create* is refused unless that exact check succeeded. The private
halves and HTTPS tokens are encrypted and never shown. The name is
`[a-z0-9][a-z0-9-]{0,63}` and unique, the URL is unique, a URL carrying a
password in its userinfo is refused, `http://` is refused, and the access kind
is derived from the URL scheme — a mismatch with the chosen radio is refused.
A fourth kind reads none at all: a folder made a bare repository under the host
directory `compose.override.yaml` mounts read-only at `/data/local-sources` is
added with a `file://` or bare-path address under that mount, "on this box" as
the access kind, and no secret ([OPERATIONS.md](OPERATIONS.md)); *Check
connection* probes that path too, refusing one outside the mount rather than
reading it. An instance starts with no source;
`PRESENTATOR_SOURCE_URL` and the other `PRESENTATOR_SOURCE_*` identity
settings are gone, so a leftover line in the environment does not add a row.
Every deck names the source that carried it, and a refresh
walks the sources one after another, taking each one in and building its decks
before it reads the next. `gitmirror` keeps a bare
mirror of each source's repository under `PRESENTATOR_MIRRORS` by driving `git` as a
subprocess ([ADR 0010](decisions/0010-git-sources-mirror.md)); nothing is ever
checked out, and the tree is read at one commit. The pull runs with a minimal
environment that cannot prompt, and inside
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS`, so an unreachable source costs one poll
that bound and no more.

A deck pushed to the source appears without anyone asking for it: the server
polls every `PRESENTATOR_SOURCE_POLL_SECONDS` on a task beside the routes, which
never runs two pulls at once and starts the next tick after the bound ends a
slow one. Where a host can call back, `POST /sources/<name>/fetch` with the
source's own secret does the same at once; it reads no payload, so every git
host and a `post-receive` hook are the same caller, and a wrong secret, a
missing secret, an unknown source, a trailing slash, and any other path under
`/sources/` answer alike. The secret is accepted from `Authorization: Bearer`
or `X-Gitlab-Token`, stored only as a SHA-256 hash, and compared in constant
time. Only that POST is open — GET and every other method under `/sources/`
stay behind the login. A flood of calls collapses
into the one refresh that runs at a time. Every poll of a source, reachable or
not, records one run: the moment, whether it reached the source, and either the
commit it found or a typed reason it did not — never the raw error a git
command left behind. A source's newest run is what the Sources list reads to show
whether it is working; older runs of that source are dropped so the table
keeps the newest three, which is what the source page shows. Opening the deck list only reads the
database. The empty deck list points an admin at Sources. A source that cannot be read is logged and says nothing about what it
carries, so every deck stays listed; a folder whose manifest cannot be read is
logged too and counts as a folder without a title, so the deck row it belongs to
stands as it was. A folder counts as a deck when it carries both `deck.toml` and
`slides.md`; its folder name is the slug and therefore its address, so changing
`title` in the manifest changes no link. A folder the source no longer carries
leaves the list: the refresh marks its deck as removed instead of deleting
anything, and a folder pushed again under the same name loses that mark and is
the deck it was, at the same address and with the same owner. Only that
source's decks are reconciled, so what one source stopped carrying says nothing
about another's. The folder name is one address for the whole instance: a
folder whose name another source already carries is skipped with a line in the
log, and the source that carried it first keeps it. Only a refresh
reconciles, so a page view never does. The most recently changed deck stands
first, and a deck belongs to the account that owns the source it came from — for
a configured source, the admin that first start created. While no deck exists,
the list says so and names the Git address instead of showing an empty table,
as long as one source is all there is to name;
there is no upload and no editing
([ADR 0005](decisions/0005-deck-folder-and-slidev.md)).

Every row carries the state of its deck's build in one word — ready, building,
failed, or never built — with a shape and a colour of its own, read off the
talk that stands and the build last attempted beside it, never stored.

`/settings/sources/<name>` is that source's page: state and fetched age, Fetch
now, the HTTPS secret as fixed dots with one Renew, or an SSH Deploy-Key with
its public half, Copy and direct Renew. SSH Renew replaces the local pair
immediately and leaves the replacement public half ready to register; it does
not update the Git host or promise uninterrupted fetching. The webhook address
has Copy and its own Renew, and the page shows the newest three runs and its
decks. Renewing the webhook secret shows the new value exactly once to the
session that created or renewed it, and never again; HTTPS renewal takes a new
value and shows nothing back. A source's secret stands in one place, the
encrypted column, opened with a key derived from `PRESENTATOR_SECRET_KEY`
([ADR 0013](decisions/0013-secrets-at-rest-and-credential-delivery.md)). A row
that still names only an environment variable stays listed and keeps its built
talks; fetch fails with a typed reason until Renew. Remove, at the bottom of
the page, asks once for a confirm naming the source's decks and runs, then
deletes the source row, its runs, its decks, its mirror, and every built talk
those decks left on disk, so the same URL can be added again straight after,
as a new source with a fresh secret.

### A deck's page, and the talk behind it

`/deck/<folder>` shows the deck's title, its folder, the state of its build,
the address it was mirrored from, the short commit the talk it delivers was
built from, and how long ago that build ran, so before speaking a person sees
whether their push is in what will be on the screen. Until a build has switched
anything over, the commit shown is the one the source last carried under that
folder. Beside the state, the page names the theme set this instance builds
with — read from the toolchain project's own `package.json`, never typed a
second time, and omitted rather than shown empty while that project cannot be
read ([ADR 0014](decisions/0014-toolchain-owns-build-dependencies.md)).

While a build runs, the page says so with the commit being built and how long
it has been running, and the two views are shown locked rather than offered:
the talk that stands may be replaced at any moment, and only a page load says
it is done. The PDF of the last good build stays offered beside them. Where the
last build failed, the page names the commit that was tried, how long ago, and
what the toolchain said — as text, never as markup, and cut to the last two
thousand characters — and says that the last talk that was built still opens;
the three ways in stay open, because the failure moved nothing (line 16). A
build that left no words at all is explained in the lobby's own sentence
instead.

Where a deck's built talk stands, which file its PDF is handed over as, which
commit both were built from, and when, are four columns on the deck's row that
one statement writes together: a reader can find the talk of one commit beside
the PDF and the build time of that same commit, never a mixture. Taking the
deck in from Git again moves none of them, so a new push never unpresents the
talk that already works and never takes away the PDF that already downloads.
A deck no build has switched over yet says so and offers no view rather than a
dead link.

While a build is pointed at, the projector view is `/deck/<folder>/` and the
presenter view `/deck/<folder>/presenter/`, both the same Slidev single-page
application: the presenter is a client-side route of that application, not a
second file the toolchain writes. A path under the deck that is not a real
file is answered with the application's `index.html`, so a wrong address
under a built deck looks like the talk rather than an inventory of what the
build wrote; a projector or presenter address under a deck that is unknown or
not built — including one whose build directory has since gone from disk —
answers a bare empty 404, with no templated page and no content type: only the
deck's own page route renders the lobby's not-found page. The slide is in the
address so a closed window comes back to the same slide. Both stand behind the
same session as every other address of
the instance ([ADR 0007](decisions/0007-browser-client-behind-tunnel.md)):
a signed-out request answers the login redirect, never a file and never a hint
that a folder exists. The address only chooses a row; the directory comes from
that row, so no part of a request becomes part of a path, and a path that would
leave the build directory — through `..`, through percent-encoded separators, or
through a symlink out of it — is refused. Every signed-in person may hold every
deck; a deck still belongs to the account that owns its source, and
per-person visibility is not ruled for this phase.

The presenter toolbar has Home for the deck overview and an AI switch; Home
turns the co-presenter off before it leaves, and neither control appears on the
projector.

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

A refresh takes the source in and then builds every deck whose commit neither
its talk was built from nor a build has already been attempted at, so a push
builds the deck it changed, leaves the other talks alone, and a deck that
cannot build costs one build rather than one per refresh until it is pushed
again. Builds run inside that refresh, which runs one at a time beside the
routes, so they follow one another and no page view waits for one.

The deck's row carries the build it last started: the commit, when it began,
whether it is running or failed, and what it said when it failed. The refresh
writes that record before the toolchain starts, so a page opened while it runs
says so; the successful switch clears it in the same statement that moves the
talk. Because builds follow one another, an attempt still saying it runs when
the next refresh reads it belongs to a process that is gone — a server
restarted mid-build — and is counted failed once it is older than
`PRESENTATOR_BUILD_TIMEOUT_SECONDS`, so nothing reads as building for ever.

A build writes the deck's tree at its commit out of the bare mirror into a
directory of that run's own under `PRESENTATOR_BUILDS` — nothing is ever
checked out into the mirror — and runs the Slidev toolchain over it: `slidev
build` against the address the talk is delivered under, then `slidev export`
for the PDF. Both write into that same run's directory, which nothing points at
while it is being written. Each step runs inside
`PRESENTATOR_BUILD_TIMEOUT_SECONDS`.

Only when both artefacts exist, and only after they are resolved and found to
stand under the builds root, does one statement switch the four columns over.
A build that failed, one that ran past its bound, one whose toolchain is not on
the machine, and one whose result stands anywhere else write no pointer at all:
the talk that already stands keeps standing and keeps its build time, and the
attempt beside it says why it is still that one. What the toolchain printed
travels back from the build as the reason rather than staying in the log, cut
to its last two thousand characters, because it is what the deck's page shows;
a run given up on, one whose tree this host could not read, and one that
printed nothing hand back no words, and the page says that in its own
sentence. What a build that did not finish left behind is taken away again, and
the directory a deck delivers from is never removed, so a request that read the previous
pointer still finds a directory. Cleaning up the builds that were pointed at is
open on [#8](https://github.com/overnightworks/agent-presentator/issues/8).

**A deck is code, and its build runs in a container of its own.** The server
asks its machine's daemon for a container per step
([ADR 0005](decisions/0005-deck-folder-and-slidev.md), line 14a): no network,
every capability dropped, none of this server's environment, and nothing of
this machine's filesystem but the deck's own tree, read-only, and the directory
that run writes — both of them directories of one volume, named after the run
rather than after anything a deck's author chose. A component that reads a file
the deck does not carry, or opens a connection, fails the build with the
toolchain's own words on the deck page, while the talk that stood keeps
standing; so does a build that wants more time, memory, processes, or disk than
one build may have. A talk larger than an instance keeps, or holding more files
than it counts, stops the step that is writing it where it stands, because a
machine is filled while a build runs and not when it ends; adding that talk up
reaches nothing by its name, follows no link a build left, and stops at
anything it cannot read. What a build writes into a file it has unlinked no
scan can see: there the step's own time and its memory are the bound, and the
container's exit frees it.

That is what an instance is, and an instance that cannot have it does not
start: without the image and the volume it builds in, on a daemon too old to
give a container one directory of a volume, on one that does not carry the
build image, or on a machine that will not hold a container's own filesystem to
a size unless the operator says to do without that bound, the composition
refuses and says which. Building on this machine instead is one explicit
setting, for a development run, and it costs the sandbox altogether. The price
of the sandbox is the docker socket, which [OPERATIONS.md](OPERATIONS.md)
names.

### Local speech

A sibling process under [`speech/`](../speech/README.md) holds a local speaking
model and a local hearing model and offers them to the host co-presenter. The
co-presenter remains a host process but is private to Presentator through a
same-UID Unix socket. The repository overlay calls relative
`/copresenter/who`, `/copresenter/ask`, and `/copresenter/hear` routes. Both
roles use them through the existing active session; `/ask` requires its CSRF
token and hearing requires the configured browser Origin. Presentator rechecks
an answer and hearing lease every second, so session loss stops generation,
capture, browser fallback and queued or playing audio. Only readiness, question,
PCM, transcript and answer events cross the private boundary; browser identity
and credentials do not. Which speech models run and what they cost on the card
remain owned by that README.

An authenticated admin can open Settings · Voice. The source surface reads one
private, local status snapshot and shows Piper, Chatterbox, Qwen3-TTS 0.6B,
VoxCPM2, and NVIDIA Magpie in that order. Downloaded Piper or Chatterbox, and
a baseline whose previous Load failed, have one Load action; the private speech
service synchronously makes that choice active and durable while current
captured speech can finish. A missing or malformed private response hides all
rows and offers Check again; a non-admin receives 403 before the private
service is called. Download, samples, and the unimplemented candidates have no
control. This describes source capability, not an inventory of any running
instance.
