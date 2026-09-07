Audience: whoever pushes a deck to a source this instance is configured to
follow.

A deck folder needs a `deck.toml` manifest naming its title (`language` is
optional) and a `slides.md` Slidev entry
([ADR 0005](https://github.com/overnightworks/agent-presentator/blob/main/docs/decisions/0005-deck-folder-and-slidev.md)).
This folder is the proof: it builds unchanged, wherever it is pushed — inside
this repository with `pnpm build:example`, and from a real source the server
follows, with no addon and no path that only resolves inside this repository.
