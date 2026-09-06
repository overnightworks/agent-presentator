# ADR 0005: A deck is a Git-tracked folder of Slidev Markdown, and the server derives the slide map from it

Audience: humans and agents adding a talk, or writing anything that reads one.

- Status: ACCEPTED 2026-09-06 — the deck store and the build are phase M0 of
  [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0006](0006-sqlite-and-files.md) puts decks and builds on the
  filesystem; [ADR 0007](0007-browser-client-behind-tunnel.md) owns the static
  build and the PDF export
- Evidence: the build-vs-reuse survey of 2026-09-06, which read Slidev's parser
  and addon mechanics at source

## Context

The tool must serve many talks, and the operator's way of adding one is to write
Markdown and commit it. Where he commits it varies and is not this product's
business: a talk may live in a personal repository, a company one, or a plain
bare repository on another machine.

The model presenting a deck needs to know what is on every slide, in order, to
narrate slide n and to answer a question about slide 14 while standing on slide
3.

The version this repository replaces obtained that knowledge from a hand-typed
constant. `SLIDE_MAP` in `chat-backend/server.py` was sixteen German strings
describing the sixteen slides of one talk, including animation counts and a
slide marked "SKIP". Editing a slide and forgetting the constant left the
presenter narrating a slide that was no longer there, and the constant made the
backend unusable for a second talk.

## Decision

The unit of work is a deck folder, tracked in Git:

- Slidev Markdown — the slides themselves.
- Assets belonging to the talk.
- A manifest naming title, language, persona, voice, and the tools this deck may
  use.

A deck source is configuration: a git URL plus a reference to one read-only
credential for that source — an SSH deploy key or an HTTPS token, one per
source. Any git remote qualifies. There is no integration with a particular
hosting product, and no hosting product is the default.

The server mirrors a source by pulling it every few minutes, and never writes
back. A source may additionally call a generic "fetch now" webhook carrying its
own per-source secret; that endpoint reads no payload from the caller, which is
what makes it host-neutral — the same URL works from a hosted service, a
self-hosted one, or a `post-receive` hook on a bare repository.

For a company-internal git the home server cannot reach, the direction reverses:
a later slice may expose a bare repository over HTTPS through the tunnel, behind
`webauth` and Cloudflare Access ([ADR 0007](0007-browser-client-behind-tunnel.md)),
so the operator pushes to it from inside. That is named here as deferred and is
not built.

The server builds the folder with Slidev and derives the slide map from the
Markdown. The slide map is never written by hand and never stored as a second
copy that can disagree with the slides.

`load()` from `@slidev/parser/fs` is the owner of that derivation. It resolves
`src:` imports and returns the slides with their speaker notes already
extracted — `SlideInfo.note` and `noteHTML` — so this repository writes no
Markdown parser and no note extraction of its own.

The presentation surface is a Slidev addon, which is a published convention and
not an invention of ours:

- The package name starts with `slidev-addon-`, and its `package.json` keywords
  contain `slidev-addon` and `slidev`.
- A deck enables it from its headmatter. A local addon needs no publishing:
  `addons: [./]`.
- Global layers — among them `global-bottom.vue`, which carries the AI
  overlay — are composed across the user, theme, and addon roots, so an addon
  may contribute one.
- `setup/main.ts` with `defineAppSetup` is the boot hook; that is where the run
  WebSocket opens.
- `useNav().go(n, clicks)` navigates. A click step is part of the address, which
  is what narration needs to address a slide mid-animation.

## Consequences

- Adding a talk is a commit and a push to a configured source. No import step,
  no upload, no admin surface.
- Editing a slide changes the narration context automatically, because both come
  from the same file.
- The manifest is the per-deck configuration seam: persona and voice belong to
  the talk, not to the installation.
- The server needs a Node toolchain to run Slidev, and a build takes time; a run
  therefore starts from a build, not from raw Markdown.
- Deriving the slide map means calling into Slidev's own parser from the Python
  side. That crossing lives in one adapter
  ([ADR 0001](0001-enforced-layers.md)); nothing above it knows Slidev exists.
- The client-side surface is bounded by what an addon may contribute. Anything
  needing a Slidev fork is out of scope by construction, which is the point.
- Deck content is only as private as the repository holding it.
- Adding a source is adding configuration and one read-only credential. Losing
  that credential exposes read access to that one source and nothing else, and
  it cannot be used to write to the operator's repository.
- Polling means a pushed change appears within minutes, not instantly. The
  webhook removes that delay where a source can call out; where it cannot, the
  delay is the cost of never opening an inbound integration.
- **A deck is code.** A Slidev deck may carry Vue components that execute at
  build time, so the build runs in an isolated container: no network, no access
  to server secrets, only the deck directory in and the build directory out.
  This holds for every deck, including the operator's own — the isolation is
  what makes "any git remote" a safe sentence.

## Rejected alternatives

- **A hand-typed slide list**, as in the previous version. It is a copy of the
  slides that has to be maintained beside them, and the failure mode is silent:
  the presenter states an outdated fact confidently on stage.
- **A pptx import.** It converts a format the operator does not author into one
  he does, and the conversion loses exactly the structure — headings, notes,
  click steps — that the narration needs.
- **An upload form.** An upload creates a copy of the deck whose version nobody
  can name, off to the side of the Git history. Decks are text; Git already
  owns text.
- **Our own Markdown parsing to derive the slide map.** It would be a second
  reader of Slidev's own format, and the two would disagree the first time
  Slidev's syntax moved.
- **Forking or patching Slidev.** Everything the overlay and the navigation
  need is public: addon global layers, `defineAppSetup`, and `useNav`.
- **A hosting-specific integration — an app installation, or a webhook parser
  that reads a particular provider's payload.** It buys nothing a `git pull`
  does not already give, and it costs the freedom to point a source at whatever
  git the next talk happens to live in. A webhook that reads no payload works
  everywhere; one that parses a payload works in one place.
