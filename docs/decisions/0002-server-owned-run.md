# ADR 0002: The server owns the presentation run as a state machine; the browser renders events

Audience: humans and agents building the presentation loop or the browser
surface.

- Status: ACCEPTED 2026-09-06 — nothing built; the run is phase M1 of
  [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0001](0001-enforced-layers.md) places the state machine in
  `application`; [ADR 0007](0007-browser-client-behind-tunnel.md) decides what
  the browser is

## Context

A live talk is a sequence of states, not a stream of independent requests. The
presenter narrates a slide, an audience member interrupts, the answer is spoken,
and the narration picks up where it stopped. Whoever holds that sequence holds
the product.

The version this repository replaces held it in the browser.
`components/AiPresenter.vue` was 1,839 lines carrying 17 reactive refs and 21
module-level mutable variables — narration flags, echo timestamps, abort
controllers, watchdog timers, a session counter to detect stale callback
chains — each of which existed to keep two flags from disagreeing. A page reload
lost the run. Nothing in it could be tested without a browser.

## Decision

The run is a state machine in `application` on the server, with five states:

`idle → narrating → listening → answering → resuming`

The server decides every transition. The browser does two things: it renders the
events the server sends, and it delivers microphone audio. It holds no
presentation state of its own and makes no decision about what happens next.

One WebSocket carries one run, with typed events in both directions. A message
is a named event with a payload, never an ad-hoc JSON shape agreed between two
call sites.

Narration for slide n+1 is generated while slide n is still being spoken. This
is what buys the latency target in [VISION.md](../VISION.md) — first audio after
a slide change under 1 s — because the next clip is already waiting when the
slide turns.

## Consequences

- The presentation loop is testable without a browser: drive the state machine
  with events and assert the events it emits.
- A page reload rejoins the run instead of ending it, because the run never
  lived in the page.
- Prefetching spends model tokens and time on a slide that the operator may skip
  past. That is the accepted cost of the latency target.
- The server holds a live session per run. The WebSocket connection and the run
  state must not be the same object, or a dropped connection kills the talk.

## Rejected alternatives

- **The state machine in the client.** This is the previous version, measured
  above. Its defect class was not sloppiness but structure: with the sequence
  spread over dozens of independent flags, every new behavior added another
  flag, and every flag added a pair that could disagree on stage.
- **Stateless HTTP requests per slide.** The narration of slide n+1 depends on
  what was actually said on slide n and on whatever the audience asked in
  between. Rebuilding that context per request means resending the transcript
  each time, which costs exactly the latency the prefetch is meant to remove.
- **Untyped socket messages.** The previous version debugged its protocol on
  stage. Typed events let [ADR 0001](0001-enforced-layers.md)'s gate hold the
  wire schema in `api`, where a reader can see the whole contract in one place.
