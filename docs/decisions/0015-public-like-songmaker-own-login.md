# ADR 0015: The instance is public like Songmaker, behind its own login, with no Cloudflare Access

Audience: humans and agents working on how the instance is reached from
outside, on the login guard, or on the pages a visitor sees before signing in.

- Status: ACCEPTED 2026-10-14 — supersedes the outer door of
  [ADR 0007](0007-browser-client-behind-tunnel.md)
- Date: 2026-10-14
- Decision authority: the operator's ruling of 2026-10-14 and the ruled
  expectation lines recorded on
  [#196](https://github.com/overnightworks/agent-presentator/issues/196)
- Neighbours: [ADR 0007](0007-browser-client-behind-tunnel.md) keeps the
  tunnel, the browser-only client, and the fallbacks;
  [ADR 0011](0011-instance-users.md) owns the accounts an admin creates;
  [ADR 0012](0012-themes-and-language.md) owns the interface's words

## Context

ADR 0007 put Cloudflare Access in front of the tunnel as an outer door and
the application login behind it as a second one. On 2026-10-14 the instance
did not run that way: the public hostname answered `/` with the redirect to
`/login`, and no Access application stood in front of it. A page reachable
from the internet without Access is a public site, and a public site needs
an Impressum and a Datenschutzerklärung a visitor can read before signing in.
The instance had neither, so the operator took the hostname off the tunnel
the same day.

Songmaker, the operator's other public application, runs the same way: its
own login, accounts created by an admin, no self-registration, and its legal
pages reachable without one.

## Decision

The instance is a public site like Songmaker. Its own login is the only door;
Cloudflare Access does not stand in front of it, and the tunnel stays the way
the home server is reached without an inbound port.

Every page, signed in or out, ends with two links. "Impressum" points straight
at the operator's one Impressum on `hallucinai.de`, so the same operator's
details have one text and no copy here. "Datenschutz" opens the instance's own
Datenschutzerklärung at `/datenschutz`, which names what this instance stores
and which AI provider receives data, because that differs from Songmaker's.
`/datenschutz` answers without a session; a signed-in reader still sees the
header there.

The declaration's words have one owner in the code,
`presentator.api.legal`, as typed sections the page renders. It is German
whatever language a reader chose: it is the operator's legal text, not
interface wording a catalog translates.

The instance goes online only after the operator has approved the
declaration's wording, every point it marks open is settled, and the pages
have been driven in a real browser at phone and desktop width.

## Consequences

- One account list, not two: adding a person is creating their account
  (ADR 0011); no allow-list has to be kept in step with it.
- Signing in and out, first start, the theme stylesheet, the source hook's
  `POST`, and the Datenschutzerklärung are reachable from the internet without
  a session.
  The login's throttle and the guard are the whole defence of everything else.
- A change to what the instance stores or sends to a provider is a change to
  the declaration in the same pull request.

## Rejected alternatives

- **Restoring Cloudflare Access in front of the login.** It is a second list
  of people to keep in step with the accounts, for an invite-only instance
  whose accounts an admin already creates; and the legal pages would still
  have to stand outside it.
- **A copy of the Impressum in this repository.** It is the same operator's
  text; a copy is the second truth that goes stale.
- **One combined Datenschutzerklärung shared with Songmaker.** Each
  application processes different data and sends it to different providers;
  a combined text would have to describe both and track both.
