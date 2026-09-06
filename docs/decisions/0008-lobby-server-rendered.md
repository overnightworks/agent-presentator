# ADR 0008: The lobby is server-rendered HTML with htmx, not an admin framework

Audience: humans and agents building the pages the operator sees before a talk
starts.

- Status: ACCEPTED 2026-09-06 — logging in and starting a deck are phase M0 of
  [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Evidence: the build-vs-reuse survey of 2026-09-06, which measured the admin
  and Python-UI frameworks named below
- Neighbours: [ADR 0007](0007-browser-client-behind-tunnel.md) owns the
  presentation surface, which this record does not touch

## Context

Before the talk there are four to six pages: log in, list the decks, look at
one, press start, and read what a past run did. They are forms, tables, and a
button. Everything visually interesting happens afterwards, in the deck.

The deck itself is a separate Slidev app. The lobby's most interesting page is
therefore "a start button that hands off to another application", which is
exactly the page no admin framework models.

## Decision

The lobby is server-rendered HTML from FastAPI: `Jinja2Templates` for pages,
[jinja2-fragments](https://github.com/sponsfreixes/jinja2-fragments) (MIT) so
one template serves both the full page and the partial that htmx swaps in,
[htmx 2.x](https://htmx.org) (0BSD, declared feature-complete) for the
interaction, and classless Pico.css for the look, from one `<link>` with no
build step.

There is no JavaScript build for the lobby, no client-side router, and no state
in the browser beyond the session cookie `webauth`
([ADR 0003](0003-libraries-for-models-and-auth.md)) sets.

## Consequences

- The lobby has no build step and no frontend toolchain. The Node toolchain
  this repository needs is Slidev's ([ADR 0005](0005-deck-folder-and-slidev.md))
  and is used for decks only.
- Authentication is the ordinary FastAPI dependency the rest of the API uses;
  no page has a second auth model to keep in sync.
- Every interaction is a request. That is the right trade for four to six pages
  and the wrong one if the lobby ever grows a live view; the run's live view is
  not here, it is the WebSocket of [ADR 0002](0002-server-owned-run.md).
- Pages and their fragments are written by hand. For this size the reuse
  benefit of a generator is negative, and this record is the place that says so
  out loud.

## Rejected alternatives

- **FastHTML.** It inherits from Starlette and is its own framework; adopting
  it means replacing FastAPI, which is a far larger decision than the lobby.
- **sqladmin and starlette-admin.** Both require SQLAlchemy models, and they
  generate CRUD over a schema. This product may talk to SQLite plainly
  ([ADR 0006](0006-sqlite-and-files.md)), and generated CRUD is not the page we
  need.
- **fastapi-admin.** It forces TortoiseORM and requires Redis, which
  [ADR 0006](0006-sqlite-and-files.md) rules out.
- **Piccolo Admin.** It is a Vue single-page application — the second frontend
  this product exists without.
- **NiceGUI and Reflex.** NiceGUI holds a persistent WebSocket per session for
  pages that are forms; Reflex compiles a React frontend and brings its own
  auth model.
- **Gradio and Streamlit.** A Gradio app mounted under FastAPI does not inherit
  the parent application's authentication, and Streamlit has no auth and re-runs
  its script on every interaction. Both are demo surfaces, and this one is
  reached from the public internet.
