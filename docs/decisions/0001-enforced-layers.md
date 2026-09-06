# ADR 0001: Six enforced layers own the code, and CI proves the import direction

Audience: humans and agents who add a module and need to know where it belongs.

- Status: ACCEPTED 2026-09-06 — the layer packages and the gate land with
  [#3](https://github.com/overnightworks/agent-presentator/issues/3)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)

## Context

The version this repository replaces had no layers. Provider calls, prompt text,
the slide list, the HTTP routes, and the audio cache lived in one FastAPI
module, so nothing could be tested without a live model key, and swapping a
speech engine meant editing the request handler.

Prose and review alone do not hold a dependency direction. A rule that a machine
can check belongs in the checks, and Import Linter already does that analysis;
this repository writes no import parser of its own.

## Decision

Six packages own the code, and a dependency may point only toward the right:

```text
host > api | adapters > application > ports > contracts
```

| Layer | Owns | May import |
| --- | --- | --- |
| `contracts` | pure types: deck, slide, run state, events | nothing internal |
| `ports` | protocols: LLM session, text-to-speech, speech-to-text, deck store, run store, tools, clock | contracts |
| `application` | presentation state machine, prompt composition, intent gate, prefetch | ports, contracts |
| `adapters` | model providers, login, speech implementations, SQLite, filesystem, Slidev build | ports, contracts |
| `api` | FastAPI routes, WebSocket wire schemas | application, contracts |
| `host` | composition root, configuration, startup | everything |

A third-party library lives only in the adapter that owns it: nothing outside
`adapters` imports a provider, an auth, a speech, or a database library. And
neither a wire schema nor a route names a port type, so the shape a browser sees
is decided by `api`, never leaked from a protocol definition.

`pyproject.toml` is the executable owner, and
`uv run --locked lint-imports` runs it locally and in CI on every pull request.
What it proves today is the direction: one `layers` contract over `host`,
`api`, `application`, `ports`, `contracts`, plus three `forbidden` contracts
that keep `adapters` from reaching up, keep everything but `host` from naming an
adapter, and keep `ports` out of `api`.

The third-party rule is not yet in that file. Per
[#3](https://github.com/overnightworks/agent-presentator/issues/3), a
library's contract is written when that library is first imported, not
speculatively before it exists. Until then the rule is a review question, and
this record says so rather than claiming a gate that is not there.

## Consequences

- The presentation logic in `application` is callable in a test with no key, no
  socket, and no GPU, because everything it touches is a protocol.
- Replacing a speech engine, a provider library, or the store is an edit inside
  one adapter.
- Adding a library means first deciding which adapter owns it. That decision
  cannot be postponed, and a shortcut import fails the pull request rather than
  a review.
- The gate proves import direction only. It does not prove that a module holds
  the right responsibility; that stays a review question.

## Rejected alternatives

- **Layers as a written convention.** This is what the previous version had in
  spirit and lost in practice within one talk's worth of edits. An unchecked
  direction is not a direction.
- **A repository-owned import checker.** Import Linter is maintained and
  already answers the question. Writing a graph walker here would add code whose
  only purpose is to distrust a library that works.
- **Fewer layers, with ports folded into application.** The whole point of the
  speech and provider decisions ([ADR 0003](0003-libraries-for-models-and-auth.md),
  [ADR 0004](0004-provider-neutral-speech.md)) is that implementations are
  interchangeable. Interchangeability needs a package that names the protocol
  and holds no implementation.
