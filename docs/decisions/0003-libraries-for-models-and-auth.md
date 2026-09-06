# ADR 0003: Model access and login come from the songmaker libraries, not from this repository

Audience: humans and agents who need a model call or a logged-in user.

- Status: ACCEPTED 2026-09-06 — depends on the extraction in
  [songmaker #825](https://github.com/overnightworks/songmaker/issues/825)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0001](0001-enforced-layers.md) confines both libraries to
  `adapters`

## Context

Two problems this product needs solved are already solved in songmaker and have
been carried by real use: talking to Claude, Codex, and Grok through the
operator's own subscriptions, and logging a person into a self-hosted web app.
Both are being extracted into standalone libraries, `agent_providers` and
`webauth`, under [songmaker #825](https://github.com/overnightworks/songmaker/issues/825).

Writing either one again here would mean maintaining a second copy of the
hardest, least product-specific code in the repository.

## Decision

`agent_providers` owns model access and `webauth` owns login. Both are consumed
as dependencies pinned by Git tag. This repository writes no provider code and
no authentication code; a defect in either is fixed in its library and pulled in
by a new tag.

What this repository does own are the ports the libraries require of their host:
the tool executor for deck tools, the settings source, and the user store. Those
are protocols in `ports`, implemented in `adapters`.

Six interface needs go to [songmaker #825](https://github.com/overnightworks/songmaker/issues/825)
as requirements of this product, because each of them is invisible in
songmaker's own batch use and unavoidable on a stage:

1. Token deltas on every Claude backend — a spoken sentence starts before the
   turn is finished.
2. Cancelling a turn in flight — the audience interrupts.
3. A tool-executor port, so this app's tools are callable by the model.
4. A long-lived session per run, not a session per request.
5. A session store that does not require Redis ([ADR 0006](0006-sqlite-and-files.md)).
6. A fake-provider harness, so the presentation loop is testable without a key.

## Consequences

- Which model presents becomes configuration, not code, and the phase M1 work is
  about the presentation loop rather than about three provider protocols.
- This repository's release cadence is coupled to the libraries': a needed fix
  arrives here only after it lands and is tagged there.
- Until the six interface needs are met, the corresponding capability is
  blocked here and its owner is the library issue, not a workaround in
  `adapters`.
- A Git-tag dependency is pinned but not published to an index. Whoever builds
  this must be able to read those repositories.

## Rejected alternatives

- **Calling each provider SDK directly from this repository.** This is what the
  previous version did with one provider, and adding the second and third is the
  work `agent_providers` exists to have already done once.
- **Copying the provider and auth code in rather than depending on it.** A copy
  is a fork the day after it is taken, and both copies then carry the same
  security-relevant surface.
- **Rolling this app's own login.** Authentication is the wrong place for a
  second implementation in a product whose entire value is on a stage.
- **Waiting for the libraries to be published to an index before using them.**
  There is one consumer and one operator; a Git tag pins a version exactly, and
  publishing buys nothing yet.
