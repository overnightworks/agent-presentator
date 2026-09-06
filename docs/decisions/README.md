# Technical decisions

Audience: humans and agents who need the durable reason for a technical choice.

This directory owns architecture decision records. A record names its context,
its decision, its consequences, and the alternatives it rejected. Other
documents may point to a record; they must not restate it as a second truth.

A record's number is the first not already taken in this directory or claimed by
an open pull request, where a pull request claims a number by the record's file
path in its diff — the landed directory alone cannot show a reservation. A
number is never reused and never renumbered.

- [ADR 0001: Six enforced layers own the code, and CI proves the import direction](0001-enforced-layers.md)
- [ADR 0002: The server owns the presentation run as a state machine; the browser renders events](0002-server-owned-run.md)
- [ADR 0003: Model access and login come from the songmaker libraries, not from this repository](0003-libraries-for-models-and-auth.md)
- [ADR 0004: Speech is a port, and the target voice runs locally on the home server GPU](0004-provider-neutral-speech.md)
- [ADR 0005: A deck is a Git-tracked folder of Slidev Markdown, and the server derives the slide map from it](0005-deck-folder-and-slidev.md)
- [ADR 0006: SQLite holds the records and the filesystem holds the files](0006-sqlite-and-files.md)
- [ADR 0007: The client is the browser alone, reached through a Cloudflare tunnel, with a static build as fallback](0007-browser-client-behind-tunnel.md)
- [ADR 0008: The lobby is server-rendered HTML with htmx, not an admin framework](0008-lobby-server-rendered.md)
- [ADR 0009: German sentence boundaries come from a library; the incremental buffer is ours](0009-german-speech-text.md)
- [ADR 0010: A reusable `gitmirror` package owns deck sources, mirroring them from any git remote](0010-git-sources-mirror.md)
- [ADR 0011: An instance has accounts an admin creates; the AI subscriptions belong to the host](0011-instance-users.md)

## How to read a record

A record is a protocol, not a running contract: its text says what was decided
on the date it names, and it is read that way. What holds today has its own
owner — [PRODUCT.md](../PRODUCT.md) owns what exists — and an accepted record is
not a claim that its slice is built.

A decision is never rewritten silently. A later ruling that changes what a
record decided lands as a dated amendment beside the passage it changes, naming
its authority, and the record's status line points a reader at it.

Each record carries its own status, and this index deliberately does not repeat
it, because a second copy of a status is the next thing to go stale.
