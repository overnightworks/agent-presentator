# ADR 0003: Model access and login come from the songmaker libraries, not from this repository

Audience: humans and agents who need a model call or a logged-in user.

- Status: ACCEPTED 2026-09-06 — amended 2026-09-07, 2026-09-08, 2026-10-20 and 2026-10-21, see below
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/presentator/issues/2)
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

### Amendment 2026-09-07 — webauth v0.2.0 owns the login mechanism

Authority: [#50](https://github.com/overnightworks/presentator/issues/50)
(slice 13a of [#8](https://github.com/overnightworks/presentator/issues/8)).

`webauth` v0.2.0 is pinned as `overnightworks-webauth` from its release wheel.
Redis is optional, hashing is a port, and session liveness is a policy, so this
host can run without Redis (ADR 0006) and without bcrypt.

This repository keeps its own truth: the SQLite user, session, and login-attempt
stores, the Argon2id hasher, first-start `/setup`, the 302 to `/login` for every
unauthenticated HTML address, the static and armed-hook exemptions, and the
one catalog sentence for a refused login. The library owns the mechanism: HMAC
cookie signing, `judge_credentials` / `login_attempt_budget`, and
the session lifetime policy. Cookie flags stay this host's
(`SameSite=Lax`, `HttpOnly`, `Secure` iff HTTPS); `issue_session_cookies` hardcodes
`Strict` and is not called. Cross-site POST refusal stays this host's Origin and
`Sec-Fetch-Site` check against the request's own origin; the library's CSRF
middleware checks an allowlist and does not read `Sec-Fetch-Site`.
`http_refusal` is not used on the HTML login: 401 vs 429 would tell throttle
apart from a wrong password.

User management (Settings · Users, password change, admin reset, deactivation)
is not in this slice. It waits on `webauth` v0.3.0 and
[#61](https://github.com/overnightworks/presentator/issues/61).

### Amendment 2026-09-08 — webauth v0.3.1 owns the public refusal and fail-closed guard

Authority: [#105](https://github.com/overnightworks/presentator/issues/105)
(slice 13c of [#8](https://github.com/overnightworks/presentator/issues/8)),
independent-review findings on the same item.

`webauth` v0.3.1 is pinned from its release wheel. It signs, sets, and clears
the session cookie through `issue_session_cookies` / `clear_session_cookies`;
`cookie_samesite` on `WebAuthConfig` (`"lax"`) is the flag they read, and
`Secure` follows the connection the library sees. `Identity.open_session`
hands out the raw session id, and `Identity.signed_in_user` remains the only
authority that decides whether that id names a person.

`CsrfOriginMiddleware` receives a `CsrfPolicy` with no explicit protected
paths, so every mutating route is protected automatically. `/sources/` is its
only exemption because its POST webhook authenticates with its own secret and
has no session. The whole-app guard calls the library's public
`unauthenticated_response(request, LoginRedirect(...))`: a browser navigation
gets `/login` with its requested path and query in `next`, while a JSON request
gets 401. It does not construct a session-store or audit stand-in merely to
obtain that answer. `webauth.middleware` and `webauth.dependencies` are no
longer banned APIs; `webauth.passwords` remains one.

### Amendment 2026-10-04 — renewal, not `load`, admits a session

Authority: [#173](https://github.com/overnightworks/presentator/issues/173)
(slice P1 of [#172](https://github.com/overnightworks/presentator/issues/172)),
closing finding 7 of [#169](https://github.com/overnightworks/presentator/issues/169).

The SQLite session store does not keep the `webauth` port's promise that `load`
holds the row until commit: each call opens its own autocommit connection. A
logout that commits between a request's `load` and its renewal must therefore
stay a logout. `touch` is a conditional `UPDATE` that never creates a row and
reports whether it renewed one; `create` is a plain `INSERT`.
`Identity.signed_in_user` names nobody when the renewal found no row, so the
renewal is the admission point.

### Amendment 2026-10-04 — webauth v0.11.0 names the cookies, the origin decides `Secure`

Authority: [#179](https://github.com/overnightworks/presentator/issues/179)
(slice P3 of [#172](https://github.com/overnightworks/presentator/issues/172)),
closing finding 4 of [#169](https://github.com/overnightworks/presentator/issues/169).

`webauth` v0.11.0 is pinned from its release wheel. The host no longer names
the session cookie: it takes the library's names, `__Host-session_id` and
`__Host-csrf_token` when the cookies are `Secure`, and the plain `session_id`
and `csrf_token` when they are not. `Secure` no longer follows the connection
the library sees: `cookie_secure` on `WebAuthConfig` is true exactly when
`PRESENTATOR_PUBLIC_ORIGIN` is `https`, so a TLS proxy that is missing from
`PRESENTATOR_TRUSTED_PROXIES` can no longer strip it. The host reads the
session id only through `session_id_from_request`, which treats a request
carrying the session cookie twice as signed out. Browsers still send the
cookie the host used to name itself, which no longer authenticates, so every
person signs in once more after the upgrade.

### Amendment 2026-10-04 — webauth's Argon2id hashes passwords

Authority: [#181](https://github.com/overnightworks/presentator/issues/181)
(slice P4 of [#172](https://github.com/overnightworks/presentator/issues/172)).

Hashing is no longer this repository's: the host constructs the library's
`Argon2idPasswordHasher()` with its default OWASP profile (m=19 MiB, t=2,
p=1), and this repository's own hasher and its hashing dependency are gone.
`webauth.passwords` stays a banned API everywhere but the host's composition,
which carries the one rule-specific exception. A hash the previous
hasher stored (Argon2id at 64 MiB, t=3, p=4) is not that profile, so a login
against it fails loudly with `UnknownHashFormatError` instead of reading as a
wrong password, and the password has to be set again.

### Amendment 2026-10-20 — `admit_session` decides, the sessions table holds no client detail

Authority: [#322](https://github.com/overnightworks/presentator/issues/322)
(K13-1a of [#242](https://github.com/overnightworks/presentator/issues/242)),
and ruling E3 (b) of [#293](https://github.com/overnightworks/presentator/issues/293).

`webauth` v0.18.0 is pinned, with `cryptography` 50.0.2 that it requires.
`admit_session` is the one authority that decides whether a session id names a
person, for the HTTP guard and for the co-presenter's WebSocket alike; it
supersedes the earlier statements that `Identity.signed_in_user` is that
authority, and the idle-window policy over `last_seen`. The host configures
`SessionLifetime(idle, cap)` from one constant, 12 hours for both, so nothing
slides: the expiry is fixed at sign-in plus 12 hours. The WebSocket Origin check stays this
host's own and runs before admission, because `admit_session` checks no
Origin.

The `sessions` table is `id, user_id, created_at, expires_at`: the library
writes the expiry, the store computes none and keeps no `last_seen`, IP address
or user agent. `load` joins the account, so a session whose account row is gone
is refused. `admit_session` never commits and the stores are autocommit, so
there is no transaction to finish; `touch` stays a conditional `UPDATE` that
reports whether it renewed a row. A refused session is no longer deleted on
read; its row stays until the purge that follows this change. The allowed
hosts are the public origin's host, because the library refuses an empty list.

### Amendment 2026-10-21 — the session row holds the refresh token, on webauth v0.20.1

Authority: [#365](https://github.com/overnightworks/presentator/issues/365)
(A3b(1) of [#355](https://github.com/overnightworks/presentator/issues/355)).

`webauth` v0.20.1 is pinned. The `sessions` table gains a nullable
`refresh_token` column, written by `create` from the code exchange and replaced
only through the update-only `replace_refresh_token`, which reports whether the
row still stood, like `touch`. The access token is never stored. The library no
longer asks the store for a hold on `load`, `touch` or `replace_refresh_token`:
each is one statement in an autocommit store, so it renews or replaces only a
row that still exists, and the host's `WriteLock` stays with the check-then-write
sequences. The session record is the identity adapter's own, with the token as a
`SecretStr`, because a third-party type lives only in the adapter that owns it
(ADR 0001); `contracts` no longer has a `Session`. A file with the earlier
four-column `sessions` table is refused untouched like every older shape: no
operated database has it, so there is no in-place migration.

### Amendment 2026-10-21 — webauth's `SessionGate` is the HTTP authority

Authority: [#376](https://github.com/overnightworks/presentator/issues/376)
(W2 of [#242](https://github.com/overnightworks/presentator/issues/242)).

`create_lobby` installs the library's middleware once, through
`install_web_auth_middleware`, in the library's order: security headers, CSRF
origin, CSRF token, session gate. This replaces the hand-written whole-app guard
of the 2026-09-08 amendment. The gate admits each gated request's session in a
worker thread, so the `touch` that renews it no longer runs on the event loop.
Its `session_transaction` is `nullcontext(sessions)` and takes no lock: each
store call commits on its own and `touch` is one update-only statement, so a lock
would only queue every page behind sign-ins. The public rules are the sign-in,
sign-out and `/setup` paths for every method, the `/static/` prefix for every
method, and the `/sources/` hook prefix for POST only; `/datenschutz` is
optional. A path with a `.` or `..` segment is never public. Pages read who is
signed in through `admitted_user`, and `request.state` carries nothing. The
co-presenter's WebSocket is not an HTTP request, so it still admits through
`admit_session` in its route.

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

From `webauth`: `SessionRecordStore` is implemented on SQLite ([ADR
0006](0006-sqlite-and-files.md)); the host builds `WebAuthConfig` from its
settings. `RateLimitPolicy` is implemented without Redis, as is the
`RateLimitBackend` port the extraction names. No `SessionCache` is implemented
here at all — see below. `BodySizePolicy` is installed with a 32 KiB default
and a 25 MiB budget for the hook prefix (amendment 2026-10-21 below); `AuditSink` is a logging sink
that writes one line per account created, role changed or back-channel logout that ended sessions. `SecurityHeadersPolicy` is
mounted: the lobby's `SecurityHeadersMiddleware` sends HSTS, a strict
`Content-Security-Policy`, and a `Permissions-Policy` that grants no device.
Only the talk host grants `microphone=(self)` — never the camera, which nothing
in this product asks for — because the co-presenter deck's own component
(`CoPresenter.vue`) is the one surface that calls `getUserMedia`, and only for
audio. `CsrfPolicy` exempts the
two sessionless addresses another server posts to, the webhook prefix and the
exact back-channel logout path; its default protects every mutating path (13c).

**No Redis.** `SessionRecordStore` and `RateLimitPolicy`
are app-supplied ports with no Redis requirement — #825 confirms that. What is
Redis today is the session cache and the sliding-window limiter, and the
extraction names them as library ports, `SessionCache` and `RateLimitBackend`,
with Redis as the shipped implementation; a non-Redis implementation is
follow-up work.

So this product's `webauth` adapter fills one of them and refuses the other. It
supplies a single-process `RateLimitBackend`, and it builds no `SessionCache`:
it passes `session_cache=None` and lets the SQLite session store be
authoritative. It starts no Redis and mounts no Redis limiter,
and [ADR 0006](0006-sqlite-and-files.md) stands unchanged.

That adapter is a **bridge, not permanent code**, and its owner is
[songmaker #835](https://github.com/overnightworks/songmaker/issues/835)
(`webauth[defaults]`: SQLAlchemy stores over a minimal schema, neutral policy
defaults, and a single-process rate-limit backend). #835 ships **no** in-memory
`SessionCache` — its default is no cache, with the database authoritative — so
the bridge above is built in that same shape rather than inventing one, and it
is deleted when #835 lands. This product is recorded there as the first caller
of the Redis-free variant.

The provider side has its own defaults item,
[songmaker #832](https://github.com/overnightworks/songmaker/issues/832)
(`agent_providers[env]`: `from_env()`, no tools, an example backend). It is
where the environment-driven provider configuration comes from and does not
touch the auth bridge.

### If the tags are late

The presentation is the product, and it does not wait on another repository's
release. If the tags slip, M0 logs in with a minimal session cookie on SQLite
behind Cloudflare Access ([ADR 0007](0007-browser-client-behind-tunnel.md)), and
M1 calls the Anthropic, OpenAI, and xAI SDKs from an adapter behind the same
`LlmSession` port. Both are replaced by the library when it lands, and neither
changes anything above `adapters`.

### Amendment 2026-10-20: autocommit stores and one write lock

`webauth`'s ports assume stores that share one transaction, which the host
commits. This host keeps autocommit stores instead: each statement commits on
its own, so there is nothing to commit or roll back. `WriteLock.hold()` takes
one `threading.RLock` per process. The sign-in callback takes it twice, for
admission and for `open_session`; the host's calls of webauth's `log_out` and
`receive_backchannel_logout` take it as well. That suffices because one
lobby process is the only writer of `users` and `sessions`
([ADR 0006](0006-sqlite-and-files.md)).

A failure between two statements leaves one of three states: an account without
a session, a signed-out browser, or ended sessions answered with 400. Each one
either fails closed or is healed by the next sign-in.

### Amendment 2026-10-21 — model access comes from the operator's agent service

Authority: [#356](https://github.com/overnightworks/presentator/issues/356)
(A3a of [#355](https://github.com/overnightworks/presentator/issues/355)),
and the operator's ruling of 2026-10-21 to use the service.

Model access comes from the operator's agent service through `agent_client`
(adapters only); this repository writes the POST itself, and `agent_providers`
is no longer the target. The co-presenter host process is retired, and its call
moves into the web process. The slice table of #355 is the plan.

### Amendment 2026-10-21: the back-channel logout and the body budget

Authority: [#382](https://github.com/overnightworks/presentator/issues/382)
(S6 of [#242](https://github.com/overnightworks/presentator/issues/242)).

`POST /auth/backchannel-logout` hands Keycloak's Logout Token to webauth's
`receive_backchannel_logout` on a worker thread, under the one write lock. A
verified token ends every session of its subject through `delete_for_user` and
is answered 200 after the delete; anything else, and any failure, is answered
400 and ends nothing, except that an audit line failing after the delete still
answers 400 (the accepted residual of the 2026-10-20 amendment). The origin
check exempts, and the gate opens for POST, exactly this path and the hook
prefix, nothing beside them. Keycloak does not use the route until its client
registers the URL (edge#50 K3c); until then a Keycloak logout elsewhere ends a
presentator session only at its twelve-hour cap.

The stack also carries webauth's body cap: 32 KiB for every route, 25 MiB for
`POST` under the hook prefix, the budget that covers the 25 MB payload cap GitHub
([webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads))
and GitLab ([webhooks](https://docs.gitlab.com/administration/instance_limits/#webhook-payload-size)) document (25 MiB
is the larger unit); Forgejo documents none, only a limit of 15 commits per push payload. A
declared length over the budget is answered 413 before the route. A body
without a length is cut where the route reads past the budget: 413 on the
back-channel, 400 on a route FastAPI parses. A sign-in that interleaves with a
back-channel logout of the same person can leave the new session open until its
cap; the remedy is a unit of work, kept in #242's "Trigger to revisit".

### Amendment 2026-10-23 — webauth v0.22.0 keeps the ID token in the session row

Authority: [#428](https://github.com/overnightworks/presentator/issues/428).

`webauth` v0.22.0 is pinned. The ID token leaves the `__Secure-oidc_id_token_hint`
cookie for a nullable `id_token` column of the `sessions` table, written by
`create`. The talk host's store is built with `keeps_id_token` off and writes
NULL on purpose, which the library's `SessionRecord.id_token` docstring does not
foresee: that host has no logout route, so a second copy of the person's claims
would serve nothing. The logout route returns `log_out`'s answer as it is, a
`no-store` page under its own policy, so the lobby's `form-action` is `'self'`
again and no end-session origin is configured. The route stays POST only and is
not in `CsrfPolicy.exempt`; if the lobby ever installs a token policy, that
policy names `/logout` in its own `token_exempt`. A file with the five-column
`sessions` table of v0.21.0 is refused untouched like every older shape: no
operated database has it, so there is no in-place migration.

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
