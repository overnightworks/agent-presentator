# ADR 0006: SQLite holds the records and the filesystem holds the files

Audience: humans and agents storing anything that must survive a restart.

- Status: ACCEPTED 2026-09-06 — the stores are phase M0 of
  [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0005](0005-deck-folder-and-slidev.md) owns what a deck is;
  [ADR 0003](0003-libraries-for-models-and-auth.md) requires a session store
  without Redis

## Context

This installation is one home server with one operator. The durable facts are
few and small: who may log in, which sessions are open, which runs happened, and
what the audience asked and got answered. The bulky things — decks, Slidev
builds, cached audio — are files that are already files.

Songmaker's infrastructure is the nearby tempting example, and it is the wrong
one: it needs Postgres, Redis, a job queue, and monitoring because it runs long
generation jobs for many users. This product has no long job. A presentation run
is interactive, is over when the talk is over, and has exactly one person
waiting on it.

## Decision

SQLite owns users, sessions, runs, and the question-and-answer log. The
filesystem owns deck folders, Slidev builds, and the audio cache.

A file's path is a record's field, not the other way round: the database never
holds a blob, and the filesystem never holds a fact the database is supposed to
answer.

Both live behind ports ([ADR 0001](0001-enforced-layers.md)), so the store is
replaceable if this decision's assumption ever stops holding.

## Consequences

- Backup is a file copy, and inspection is one `sqlite3` command.
- There is no separate service to start, upgrade, or monitor beside the app.
- SQLite's single writer is enough for one operator and one run; concurrent
  talks by different users would not be, and that is the trigger to revisit.
- The audio cache grows on disk with nothing evicting it until something is
  written to do so.
- This record does real work when other decisions are taken. In the
  build-vs-reuse survey of 2026-09-06 it is the reason three otherwise
  reasonable candidates were rejected: LiveKit's media server
  ([ADR 0002](0002-server-owned-run.md)), fastapi-admin's Redis requirement
  ([ADR 0008](0008-lobby-server-rendered.md)), and a TURN server for WebRTC
  ([ADR 0007](0007-browser-client-behind-tunnel.md)).

## Rejected alternatives

- **Postgres.** A second service and a second thing to operate, bought for
  concurrency that one operator does not generate.
- **Redis.** Nothing here needs a shared in-memory store, and requiring one
  would make the app un-runnable on a laptop; this is why
  [ADR 0003](0003-libraries-for-models-and-auth.md) asks the session library for
  a store without a Redis requirement.
- **A job queue.** A queue exists to defer work past the caller's patience. Here
  the caller is the operator on stage: nothing may be deferred, and a run that
  is not immediate has already failed.
- **A monitoring stack.** One machine, one user, and logs he can read.
