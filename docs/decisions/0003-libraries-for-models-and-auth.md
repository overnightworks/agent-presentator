# ADR 0003: Model access and login come from the songmaker libraries, not from this repository

Audience: humans and agents who need a model call or a logged-in user.

- Status: ACCEPTED 2026-09-06 — depends on the extraction in
  [songmaker #825](https://github.com/overnightworks/songmaker/issues/825)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0001](0001-enforced-layers.md) confines both libraries to
  `adapters`; [ADR 0006](0006-sqlite-and-files.md) forbids the Redis this
  product does not start; [ADR 0011](0011-instance-users.md) owns who the users
  are

## Context

Two problems this product needs solved are already solved in songmaker and have
been carried by real use: talking to Claude, Codex, and Grok through the
operator's own subscriptions, and logging a person into a self-hosted web app.
Both are being extracted into standalone libraries, `agent_providers` and
`webauth`, under [songmaker #825](https://github.com/overnightworks/songmaker/issues/825).

Writing either one again here would mean maintaining a second copy of the
hardest, least product-specific code in the repository.

What #825 is, exactly, matters to this decision. It is a one-to-one extraction
with no feature growth: behavior stays identical, proven by the tests that move
with the code. Its own rule is that a seam is allowed while it runs and
follow-on work is not. So this repository builds against what the first tag
delivers, not against what the libraries might grow into.

## Decision

`agent_providers` owns model access and `webauth` owns login. Both are consumed
as dependencies pinned by Git tag. This repository writes no provider protocol
and no authentication mechanics; a defect in either is fixed in its library and
pulled in by a new tag.

### What exists today, and what does not

Six interface needs were filed against #825 as
[a comment](https://github.com/overnightworks/songmaker/issues/825#issuecomment-5558849693)
and classified by its head in the issue body under "Anforderungen
agent-presentator". They are wishes, not promises, and that classification —
not this record — is the source of truth:

| Need | Classification on #825 |
| --- | --- |
| A tool executor for this app's tools | Fulfilled — `ToolExecutor` |
| A long-lived session per run | Partly fulfilled — `ToolTransport` is resumable; duplex later |
| Token deltas on every Claude backend | Follow-up work on the library |
| Cancelling a turn in flight | Follow-up work on the library |
| A session store that does not require Redis | Partly — see below |
| A fake-provider harness | Follow-up work on the library |

The follow-up items are the same list atelier-2 filed, so this product is not
alone in waiting for them. This record assumes none of them.

### What this repository may build meanwhile

The ports are ours ([ADR 0001](0001-enforced-layers.md)), so what fills them is
ours to choose. Until the libraries grow the missing pieces, this repository may
put behind its own `LlmSession` port, in `adapters` and in `tests`: test doubles
for the provider ports, so the state machine of
[ADR 0002](0002-server-owned-run.md) is testable without a key, and a
turn-cancel wrapper that stops consuming a stream and abandons the turn. Both
are this app's code behind this app's protocol, not a second implementation of
provider access.

### The ports this app implements

`ports` holds this app's protocols only. The library types below are named by
the libraries and are implemented in `adapters`, which is the only layer that
imports either library.

From `agent_providers`: `ProviderRuntimeConfig`, `SecretEnvKeys`,
`ToolExecutor`, and `ToolCatalog`. `McpServerSpec` and `ImagePolicy` go unused —
this product runs no MCP server and generates no images.

From `webauth`: `WebAuthConfig`, `UserStore`, `SessionRecordStore`, and
`LoginAttemptStore` are implemented on SQLite
([ADR 0006](0006-sqlite-and-files.md)). `RateLimitPolicy` is implemented without
Redis, as are the `SessionCache` and `RateLimitBackend` ports the extraction
names. `AuditSink`, `CsrfPolicy`, `BodySizePolicy`, and `SecurityHeadersPolicy`
are taken with their defaults and stubbed until something here needs them.

**No Redis.** `SessionRecordStore`, `LoginAttemptStore`, and `RateLimitPolicy`
are app-supplied ports with no Redis requirement — #825 confirms that. What is
Redis today is the session cache and the sliding-window limiter, and the
extraction names them as library ports, `SessionCache` and `RateLimitBackend`,
with Redis as the shipped implementation; a non-Redis implementation is
follow-up work.

So this product's `webauth` adapter supplies SQLite implementations of
`SessionCache` and `RateLimitBackend` itself. It starts no Redis and mounts no
Redis limiter, and [ADR 0006](0006-sqlite-and-files.md) stands unchanged.

Those two implementations are a **bridge, not permanent code**. #825 carries a
defaults layer as follow-up row F2 — `agent_providers[env]` and
`webauth[defaults]` with an in-memory cache and limiter, SQLAlchemy stores, and
policy defaults — scheduled after the repository split. This product is recorded
there as the first caller of the Redis-free variant. When F2 lands, these
adapters are deleted in favour of it; they are written to be replaced and are
listed against that row, not defended.

### If the tags are late

The presentation is the product, and it does not wait on another repository's
release. If the tags slip, M0 logs in with a minimal session cookie on SQLite
behind Cloudflare Access ([ADR 0007](0007-browser-client-behind-tunnel.md)), and
M1 calls the Anthropic, OpenAI, and xAI SDKs from an adapter behind the same
`LlmSession` port. Both are replaced by the library when it lands, and neither
changes anything above `adapters`.

## Consequences

- Which model presents becomes configuration, not code, and the phase M1 work is
  about the presentation loop rather than about three provider protocols.
- This repository's release cadence is coupled to the libraries': a needed fix
  arrives here only after it lands and is tagged there. The fallback above is
  what keeps that coupling from being a stop.
- No reimplementation of provider access or auth mechanics happens here. Code
  *near* them — doubles, a cancel wrapper, a SQLite store — is ours and expected,
  as long as it sits behind our own port.
- A Git-tag dependency is pinned but not published to an index. Whoever builds
  this must be able to read those repositories.

## Rejected alternatives

- **Calling each provider SDK directly as the target.** This is what the
  previous version did with one provider, and adding the second and third is the
  work `agent_providers` exists to have already done once. It stays available as
  the fallback above, which is a different thing from a target.
- **Copying the provider and auth code in rather than depending on it.** A copy
  is a fork the day after it is taken, and both copies then carry the same
  security-relevant surface.
- **Rolling this app's own login.** Authentication is the wrong place for a
  second implementation in a product whose entire value is on a stage.
- **Taking `webauth` with its Redis path.** It would reintroduce the store
  [ADR 0006](0006-sqlite-and-files.md) rejected, for a single operator. The
  app-supplied store ports are the reason this is a choice and not a
  requirement.
- **Waiting for the libraries to be published to an index before using them.**
  There is one consumer and one operator; a Git tag pins a version exactly, and
  publishing buys nothing yet.
