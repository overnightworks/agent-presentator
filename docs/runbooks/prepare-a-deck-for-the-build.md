# Runbook: prepare a talk authored for another Slidev setup

Reader: a deck author whose talk builds elsewhere but fails here.

## Before you start

A deck folder carries no dependencies of its own; the toolchain project owns
them ([ADR 0014](../decisions/0014-toolchain-owns-build-dependencies.md)). The
deck's own page names the theme set this instance builds with.

## Steps

1. A theme, addon or plugin builds only if `frontend/` carries it too; widen
   that set through a change to `frontend/package.json`, not a push of the
   deck.
2. Strip a Vue component wired to a service the deck does not bring with it:
   the build has nowhere to call, and a deck's own Vue components and global
   layers run in the build.
3. Turn a remote asset named in the frontmatter, such as a background image
   fetched by URL, into a local file committed into the deck folder and named
   by a relative path: a container build never gets network.

## Check

Push the deck and open its page: the build ends ready. A failure shows the
toolchain's own words on the deck page, and the talk that stood keeps standing.
