# ADR 0011: An instance has accounts an admin creates; the AI subscriptions belong to the host

Audience: humans and agents building login, the first-run setup, or anything
that asks who owns a thing.

- Status: ACCEPTED 2026-09-06 — first-run setup and login are phase M0 of
  [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06, filed against
  [songmaker #825](https://github.com/overnightworks/songmaker/issues/825#issuecomment-5559001283)
- Neighbours: [ADR 0003](0003-libraries-for-models-and-auth.md) owns the login
  mechanics; [ADR 0010](0010-git-sources-mirror.md) requires an owner per deck
  source

## Context

The operator is the person this tool is built for, but he is not the only
possible person: an instance should be self-hostable by someone else, with their
own subscriptions and their own users. That has to be true from the first
schema, because retrofitting an owner column onto rows that have none is the
migration nobody wants.

Songmaker has already answered this question in production, and the operator's
ruling is to take the same answer rather than invent a second one.

## Decision

The AI subscriptions belong to the instance host, not to individual users. Who
presents does not change whose subscription pays.

An admin creates accounts. There is no self-registration. The first start of a
fresh instance creates the admin, and that is the only way an account appears
without one. There are two roles, `admin` and `user`, and no third. Users live
as a tab under Settings, and personal preferences live under Account
([#8](https://github.com/overnightworks/agent-presentator/issues/8) line 23,
2026-09-06).

Decks and deck sources carry an owner from day one
([ADR 0010](0010-git-sources-mirror.md)).

Any instance is self-hostable: nothing in this model assumes one particular
person or one particular deployment.

### Where this lives

The mechanics come from `webauth` ([ADR 0003](0003-libraries-for-models-and-auth.md)).
The user model, the first-run setup, and the admin routes do not: songmaker
keeps those today, so every new project would rebuild exactly this part. That is
a task child of #825 —
[songmaker #833](https://github.com/overnightworks/songmaker/issues/833),
`webauth[users]`: first-run setup, an admin who creates users, the two roles,
deactivation, and ending sessions — with this product recorded as its first
caller.

Until #833 lands, this repository builds the first-run setup minimally against
the `webauth` ports. It is a **bridge owned by #833**, not this repository's
code to keep: it is listed against that item and deleted when the library ships
it.
Minimally means what M0 needs to log in and no more — no admin console, no user
administration surface built here to be deleted later.

## Consequences

- Every durable row that belongs to somebody has an owner from its first
  migration, and no later change has to invent one.
- The first-run setup is code written to be deleted, and songmaker #833 is the
  owner that deletes it. Naming that out loud is what keeps it small.
- Two roles are enough until something needs a third, and that something has to
  argue for itself against this record.
- No self-registration means an instance cannot be joined by a stranger who
  finds the URL, which matters more than usual for a surface reachable through a
  public hostname ([ADR 0007](0007-browser-client-behind-tunnel.md)).

## Rejected alternatives

- **A single-user instance with no account model.** It is what one operator
  needs today and it is the migration that hurts most later: owners, roles, and
  first-run setup all arrive at once, on data that predates them.
- **Self-registration.** The instance is reachable from the internet, and there
  is no case for a stranger creating an account on the operator's home server.
- **OAuth or an external identity provider.** It adds a dependency and a flow
  for a handful of accounts on a self-hosted instance, and Cloudflare Access
  already stands in front of the door.
- **Per-user AI subscriptions.** The subscriptions are the host's, and metering
  them per user would be an accounting product this one is not.
- **Roles beyond `admin` and `user`.** No caller needs a third role. Adding one
  now would be a permission model built ahead of its first question.
