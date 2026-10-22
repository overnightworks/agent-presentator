# Sign-in and session reference

Audience: an engineer or operator looking up how a person gets in and what
protects a request. Setup of the Keycloak client is
[configure Keycloak](../runbooks/configure-keycloak.md); the settings are in
[configuration](configuration.md).

## Sign-in

There is no password login and no `/setup`; accounts arrive with the Keycloak
sign-in, and no password is stored.

- `/login` says "You are logged out." with one Log in button and the legal
  links. The button starts the sign-in at `/login/oidc`, which answers 303 to
  the issuer with PKCE, `state` and `nonce`.
- The return at `/auth/callback` admits only members of the Keycloak group
  `/apps/presentator` (admins: `/apps/presentator/admin`), opens one session,
  sets the cookies once and lands on the requested path or `/`. The groups
  mapper must send full paths (`full.path` on).
- A person who signs in at Keycloak but is not in that group sees "No access"
  (Log out and Back, nothing written). Every other refused sign-in shows one
  "Login failed" page with a Try again link. A failing store answers 500.
- A sign-in writes an INFO log line when it creates an account or changes its
  role (event kind, Keycloak subject id, role).
- A signed-out browser navigation is redirected to
  `/login/oidc?next=<requested-path-and-query>`; a JSON request receives 401.
- Logging out is a POST that deletes the session row at once and sends the
  browser to Keycloak's end-session endpoint (with the ID-token hint when
  usable), which returns it to `<PRESENTATOR_PUBLIC_ORIGIN>/login`.
- A valid Logout Token at `POST /auth/backchannel-logout` ends every session
  of its subject, on every device.

The talk host signs in by itself ([ADR 0016](../decisions/0016-a-talk-host-signs-in-by-itself.md)):
the same client, the redirect URI `<PRESENTATOR_TALK_ORIGIN>/auth/callback`, and
its own host-only `__Host-session_id`, signed under a key derived for it. It
serves only the signed-in owner's talk under `/deck/<slug>/`, sends `/` to the
lobby, and answers every lobby route with 404. A refused talk sign-in is
redirected to `/login/talk/no-access` or `/login/talk/login-failed` on the
lobby; Try again restarts the sign-in at the talk host. A Logout Token ends the
sessions of both hosts, a logout at the lobby ends the lobby's alone.

## Session

A session that SQLite holds is carried by a signed cookie and ends twelve
hours after the sign-in that opened it, however often it is used; the store
keeps the sign-in time and the expiry for that. The session row holds no
client address and no user agent, and holds the sign-in's refresh token in
plain text ([ADR 0013](../decisions/0013-secrets-at-rest-and-credential-delivery.md)).
A session past its expiry is refused, and a schedule of its own deletes
expired rows every five minutes, apart from the deck refresh.

The SQLite stores stay this repository's
([ADR 0003](../decisions/0003-libraries-for-models-and-auth.md),
[ADR 0011](../decisions/0011-instance-users.md)).

## What protects a request

`webauth` v0.21.0 owns the mechanism. `issue_session_cookies`,
`clear_session_cookies` and `session_id_from_request` set, clear and read the
signed `SameSite=Lax` session cookie.

- `SessionGate` refuses a gated request without a session (the public and
  optional paths are the exceptions) and admits the others in a worker thread.
  `admit_session` is the one authority, for HTTP and WebSocket alike (the
  co-presenter's WebSocket admits through it in its own route), that decides
  whether the cookie's session id names a person.
- `BearerGate` takes the prefix `/api/admin/` by an access token alone and
  never by the cookie (see [people](people.md)); it exists only while
  `PRESENTATOR_ADMIN_TOKEN_CLIENTS` names a client.
- A request that carries the session cookie twice is treated as signed out
  rather than letting either copy decide whose account it runs in.
- The public origin is https, so both cookies are `Secure` and carry the
  host-only names `__Host-session_id` and `__Host-csrf_token`, which only this
  exact host can set.
- Every mutating path is cross-origin protected, fail-closed, unless exempted;
  only the sessionless webhook prefix `/sources/` and the exact path
  `/auth/backchannel-logout` are exempt. A request with `Sec-Fetch-Site:
  cross-site` or `none` is refused outright, and so is a form POST carrying
  neither `Sec-Fetch-Site` nor `Origin`/`Referer`. A request without
  `Sec-Fetch-Site`, or with `same-site`, is judged by its `Origin` (or
  `Referer`) against the hostname of the origin the app answers for
  (`PRESENTATOR_PUBLIC_ORIGIN` for the lobby, `PRESENTATOR_TALK_ORIGIN` for the
  talk host), the only host its allowlist names (`allowed_hosts_exact`): a form
  naming it is admitted, any other host, the other app's included, is refused.
  The match is by hostname, so no party may be given another port or scheme of
  either hostname ([webauth#401](https://github.com/overnightworks/webauth/issues/401)).
  A direct run must send the public `Host`. The guard does not depend on the
  trusted-proxy list.
  A native call to a route that needs a session answers as any missing session
  does.
- Request bodies are capped at 32 KiB, and `POST /sources/<name>/fetch` at
  25 MiB, a budget above the 25 MB payload cap that
  [GitHub](https://docs.github.com/en/webhooks/webhook-events-and-payloads) and
  [GitLab](https://docs.gitlab.com/administration/instance_limits/#webhook-payload-size)
  document, as 25 MiB is the larger unit. A larger declared length gets 413.
- Every answer, including the sign-in redirect and a refusal, carries
  `webauth`'s `SecurityHeadersMiddleware` headers: `Cache-Control: no-store`
  (no answer may be replayed from the browser cache), a
  `Content-Security-Policy` admitting only this origin's own scripts, styles
  and connections (the lobby admits no inline style; the talk host does,
  because a built Slidev talk inlines its styles), and a `Permissions-Policy`
  granting `microphone=(self)` on the talk host for the co-presenter deck and
  nothing else; the lobby sends `microphone=()`. `Strict-Transport-Security`
  follows the always-on `Secure` cookie setting, so it is always sent.

## Legal links

Every lobby page, signed in or out, ends with two links: Impressum goes to the
operator's one Impressum at `https://hallucinai.de/impressum`, Datenschutz to
`/datenschutz`. That page answers without a session and shows the German
Datenschutzerklärung that `presentator.api.legal` owns
([ADR 0015](../decisions/0015-public-like-songmaker-own-login.md)).
