# ADR 0016: A talk host signs in by itself, as a second app of the same process

Audience: humans and agents working on where a deck's talk runs, on the
session cookies, or on the sign-in.

- Status: ACCEPTED 2026-10-22
- Date: 2026-10-22
- Decision authority: the operator's rulings recorded on
  [#400](https://github.com/overnightworks/presentator/issues/400) (container
  sentences Host, Design and Legal) and the slice
  [#414](https://github.com/overnightworks/presentator/issues/414)
- Neighbours: [ADR 0003](0003-libraries-for-models-and-auth.md) owns the
  library and the SQLite stores; [ADR 0013](0013-secrets-at-rest-and-credential-delivery.md)
  owns what the instance key derives; [ADR 0015](0015-public-like-songmaker-own-login.md)
  owns the pages a visitor sees before signing in

## Context

A deck is code, and its built talk ran under the lobby's origin and session.
Anything the deck's code could do there, it could do as the signed-in person
to the lobby.

## Decision

The talk lives on an origin of its own, `PRESENTATOR_TALK_ORIGIN`, another
hostname than the lobby's. One process and one uvicorn server answer both: a
router chooses the app by the `Host` header's hostname and answers any other
`Host`, or none, with 404, for HTTP and for WebSockets. Neither app is
reachable under the other's name.

- **Its own relying party.** The talk host signs in through the same Keycloak
  client at its own redirect URI `<talk>/auth/callback`; signing out returns
  to the lobby, the only place to log out.
- **Its own cookies and key.** Each app has its own `WebAuthConfig`, whose
  allowlist names only its own hostname, so each refuses the other's Origin.
  The talk host signs its host-only `__Host-session_id` with a key derived
  from the instance key under its own HKDF purpose; the lobby keeps the raw
  key (ADR 0013). A cookie, a pending login or a state of one host therefore
  never verifies on the other.
- **One store, one lock.** Both apps write the one `sessions` table, take the
  one write lock, use the one account store and report to the one audit sink,
  so a verified Logout Token at the lobby ends the talk sessions too.
- **A refusal shows on the lobby.** The talk host has no page of its own. A
  refused talk sign-in is a redirect to `/login/talk/no-access` or
  `/login/talk/login-failed` on the lobby, whose Try again restarts the
  sign-in at the talk host.
- **One owner check.** The talk host serves a talk through the same
  owner-checked `built_talk` as the lobby and compares no owner itself.

## Consequences

- The Keycloak client registers both callbacks, and the deployment names the
  talk origin and routes its hostname to the same listener.
- Two hostnames are two origins on one site: no cookie, route or Origin of one
  is honoured by the other, up to two named residuals. A hostname without a
  port matches every port and scheme of that hostname
  ([webauth#401](https://github.com/overnightworks/webauth/issues/401)). The
  hosts are same-site with each other and with the sibling apps, so SameSite
  does not separate them and deck code on the talk host can still set
  `Domain=` cookies for the shared registrable domain.
- The Datenschutzerklärung names the talk host's own session cookie.
- The co-presenter routes and the microphone permission are the talk host's;
  the lobby keeps its talk mount until it stops serving talks.

## Rejected alternatives

- **A second process.** It would need a second write lock over the one SQLite
  file; a double lock is the corruption the single process rules out.
- **One cookie across both hosts.** A deck's code would then run next to the
  credential it must not reach.
- **A refusal page on the talk host.** It would need the lobby's footer and
  catalog on an origin that serves nothing else.
