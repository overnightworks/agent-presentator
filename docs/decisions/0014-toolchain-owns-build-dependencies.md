# ADR 0014: The toolchain owns a deck's build-time dependencies; a deck declares none

Audience: humans and agents writing a deck, or writing anything that builds
one.

- Status: ACCEPTED 2026-09-08 — the deck page shows the theme set this
  instance builds with
- Date: 2026-09-08
- Decision authority: the operator's ruling of 2026-09-08 after an
  independent plan review, recorded on
  [#85](https://github.com/overnightworks/agent-presentator/issues/85)
- Neighbours: [ADR 0005](0005-deck-folder-and-slidev.md) owns that a deck is
  code and its build is sealed; [ADR 0012](0012-themes-and-language.md) owns
  the lobby's own appearance, a different theme from a deck's own;
  [#88](https://github.com/overnightworks/agent-presentator/issues/88) owns
  which themes ship; [#69](https://github.com/overnightworks/agent-presentator/issues/69)
  owns the sealed build this record assumes

## Context

A deck is code, and its build runs with no network
([ADR 0005](0005-deck-folder-and-slidev.md), line 14a): `--pull=never`, so
nothing a build asks for can be fetched while it runs. A talk written for
another Slidev setup can still name a theme or an addon its author installed
there, and until now nothing told a deck's author what this instance's build
actually carries before they pushed. [#71](https://github.com/overnightworks/agent-presentator/issues/71)
proved the failure: the operator's March talk named `@slidev/theme-seriph`,
and the first anyone learned the theme was missing was the toolchain's own
words on a failed build.

## Decision

A deck names a theme or an addon only from what the toolchain image already
carries. `deck.toml` gains no dependency key: a declared allowlist there
cannot widen a sealed image, and it would duplicate Slidev's own
frontmatter for saying the same thing a second way; letting a build install
on demand needs registry network, which line 14a already refuses. The set has
one owner, this repository's `frontend/package.json`, and every reader
derives it rather than typing it a second time.

The deck page names the themes this instance builds with, read from the
toolchain project's `package.json` at `PRESENTATOR_TOOLCHAIN`: every
`@slidev/theme-*` package its `dependencies` or `devDependencies` carry,
stripped of that prefix, plus `default`, which ships with Slidev itself.
`presentator.contracts.decks.theme_names` is the one reader of that shape;
`presentator.adapters.builds.PackageJsonThemes` is the one place that opens
the file, at every deck-page view rather than once at start — the file is
small and local, and a value cached at start would need its own invalidation
the moment a deployment's toolchain changes without a restart, for a cost
this read does not have. A project this server cannot read, or a manifest
that is not valid JSON, is not a set to derive a guess from: the page shows
no row at all rather than an empty one that would read as "no themes".

## Consequences

- Widening the set is one change to `frontend/package.json` and its
  lockfile, reviewed and rebuilt like any dependency change — an image
  rebuild and a redeploy, never a push to a deck's own source.
- Two instances can carry different sets, so the page names which set this
  one has rather than assuming the operator's own.
- No build-time registry access ever exists; a deck that named something
  outside the set still fails the way [#71](https://github.com/overnightworks/agent-presentator/issues/71)
  found it, with the toolchain's own words on the deck page.
- A deck author reads the set before pushing instead of after a failed
  build, which is what this record was written to fix.

## Named gap

The product's own sentence naming an unknown theme on a failed build, rather
than only the toolchain's words, is not built here. Its caller is the second
deck that trips it; its owner, once built, is Slidev's `load()`
([ADR 0005](0005-deck-folder-and-slidev.md)), never a hand-matched copy of
the toolchain's output text.

## Rejected alternatives

- **An allowlist in `deck.toml`.** It cannot widen what a sealed image
  carries, so it would only duplicate Slidev's own frontmatter for saying the
  same theme a second way, with the two free to disagree.
- **An on-demand install with registry network.** It contradicts the sealed
  build's no-network line (ADR 0005, line 14a) and reopens exactly the
  surface that line closes.
- **A build image per deck.** It would let each deck's push widen its own
  set, which is the allowlist problem again, paid for in an image per deck
  instead of one dependency file.
