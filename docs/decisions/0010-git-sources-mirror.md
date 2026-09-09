# ADR 0010: A reusable `gitmirror` package owns deck sources, mirroring them from any git remote

Audience: humans and agents adding a deck source, or building the second caller
of this package.

- Status: ACCEPTED 2026-09-06 — `src/gitmirror` mirrors one source, which the
  server polls and a hook can hurry; the host owns credential storage and
  rotation
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0005](0005-deck-folder-and-slidev.md) owns what a deck is and
  builds it; [ADR 0001](0001-enforced-layers.md) owns the boundary this package
  sits beside

## Context

A deck is a folder in a git repository ([ADR 0005](0005-deck-folder-and-slidev.md)),
and where that repository lives is not this product's business: a personal
remote, a company one, or a bare repository on another machine. Tying the
mechanism to one hosting product would decide where every future talk has to
live.

Mirroring a remote is also not a presentation problem. It is credentials,
polling, a webhook, a fetch log, and an owner per source — the same problem
atelier-2 has for its own sources. Songmaker already demonstrates the shape this
should take: `acestep_engine` is an independent package inside its repository
with an enforced import boundary, which is what let
[songmaker #825](https://github.com/overnightworks/songmaker/issues/825) lift
two libraries out later without a rewrite.

## Decision

`gitmirror` is an independent package in this repository, `src/gitmirror`, with
its own top-level name, beside the layered tree of
[ADR 0001](0001-enforced-layers.md) rather than inside it. An import-linter
contract forbids any import from `presentator`, so the dependency runs one way
only, and a second one keeps every layer but an adapter from naming the package.
When a second caller
arrives, the package moves to its own repository and is consumed by tag, exactly
as `agent_providers` and `webauth` are ([ADR 0003](0003-libraries-for-models-and-auth.md)).

It owns:

- **A source as configuration** — a git URL, ref, and optional read-only
  credential reference. It asks the host to resolve that reference at fetch
  time; it does not store, generate, or rotate a credential. Any git remote
  qualifies; no hosting product is the default.
- **Pull-based mirroring** — polling on an interval, plus a generic "fetch now"
  webhook carrying its own per-source secret. That endpoint reads no payload
  from its caller, which is what makes it host-neutral: the same URL works from
  a hosted service, a self-hosted one, or a `post-receive` hook on a bare
  repository. The mirror never writes back.
- **A "commit arrived" event** and a connection check. Source ownership,
  credential storage and rotation, and the fetch log belong to the caller.

Connection results carry one closed next-action vocabulary derived from their
state: ready needs no action, an unresolvable credential needs resolving,
refused access needs repair, an unreachable source needs reachability checked,
and another failure needs inspection. Callers receive this typed action rather
than infer it from a result's operational detail.

It does not own building. Turning a mirrored folder into a presentable deck is
this product's job ([ADR 0005](0005-deck-folder-and-slidev.md)), and the deck
build's container isolation is stated there.

Credential vocabulary follows atelier-2's
[ADR 0017](https://github.com/FlexOr2/atelier-2/blob/main/docs/decisions/0017-account-credential-model.md),
which is still PROPOSED there and is adopted here as vocabulary, not as a
finished contract. Its split, in its own terms: an **Account** is the
installation-owned record of one connected external identity, holding the
provider, the auth mode, the credential *source*, and exactly one credential
anchor — a reference. The durable application database stores that record and
**never** a secret value. Where the value itself lives is a separate axis:
server-held, or runner-local, where the server holds no secret material at all.
Its stated preference is *reference-before-vault* — hold a reference wherever
one exists, and store a raw secret only where no delegated form is offered.

For a deck source, both credential forms are **server-held stored secrets**:
the deploy key's private half and the pasted token live on this one machine, so
the source axis does not separate them, and both are encrypted at rest. This
product borrows 0017's vocabulary and its reference discipline, not its
runner-local mode — that mode means the server holds no secret material at all,
and it would apply here only if some other host held the value. Using the same
words from the start is what lets the two trees merge later without a
translation layer.

### Callers

This product is the first caller. atelier-2 is the second, with two items —
[#660](https://github.com/FlexOr2/atelier-2/issues/660) for definition sources
(agents, skills, and workflows referenced from a git repository) and
[#567](https://github.com/FlexOr2/atelier-2/issues/567) for project sources.
Their needs are the contract the first cut must already satisfy, because a seam
that ignores the known second caller is a rewrite:

1. The "commit arrived" event carries the ref and the commit hash.
2. The tree is readable at exactly that commit, as file bytes, with no
   working-copy guarantee — so a caller can publish content-addressed revisions.
3. Every source has an owner.
4. The connection check returns a typed result — `ready`,
   `credential-unresolvable`, or `unreachable` — never a string to be parsed.
5. Credentials reach the library only through a host-resolved reference, per
   atelier-2 ADR 0017. Storage and rotation remain outside the library.

Neither caller needs a write path. Pushing and opening pull requests stay
atelier-2's own git-transport concern, and this product never writes back.

### Deferred, with an owner

For a company-internal git the home server cannot reach, the direction reverses:
a bare repository exposed over HTTPS through the tunnel, behind `webauth` and
Cloudflare Access ([ADR 0007](0007-browser-client-behind-tunnel.md)), that the
operator pushes to from inside. It is owned by
[#8](https://github.com/overnightworks/agent-presentator/issues/8), line 4a, and
is not built here.

## Consequences

- The mirror is testable and shippable on its own, against a local bare
  repository, with no presentation code in the loop.
- The import boundary costs discipline now and buys the move later. It is the
  same trade songmaker made, and the evidence there is that it paid.
- Two callers means the first cut is designed against six external needs rather
  than one product's convenience. That is more design work up front and less
  rework than discovering them at the move.
- Polling means a pushed change appears within minutes, not instantly, wherever
  a source cannot call the webhook.
- Holding both credential forms on this machine means this product holds
  secrets at rest, with encryption, a rotation path, and a fetch log as the
  visible controls. Nothing here is credential-minimal in 0017's sense, and the
  record says so rather than borrowing a word that would suggest otherwise.

## Rejected alternatives

- **An OAuth or provider app installation.** It carries delegated user tokens
  through a provider-specific flow, is usually write-capable when read is all
  that is needed, and simply does not exist for a bare git repository — the case
  a self-hosted tool has to cover.
- **A webhook that parses a provider's payload.** It would work in one place. A
  webhook that reads no payload works everywhere, and the per-source secret does
  the authenticating either way.
- **Building the mirror inside `presentator`.** It would be reachable from the
  presentation layers, and the second caller would then get a rewrite instead of
  a dependency.
- **Waiting for the second caller before making it a package.** The boundary is
  cheap to hold from the first line and expensive to retrofit; atelier-2's two
  items are named callers today, not speculation.
