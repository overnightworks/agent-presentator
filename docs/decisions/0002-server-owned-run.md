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
- Evidence: the build-vs-reuse survey of 2026-09-06, which measured the
  voice-agent frameworks named under rejected alternatives

## Context

A live talk is a sequence of states, not a stream of independent requests. The
presenter narrates a slide, an audience member interrupts, the answer is spoken,
and the narration picks up where it stopped. Whoever holds that sequence holds
the product.

The version this repository replaces held it in the browser.
`components/AiPresenter.vue` was 1,839 lines carrying 19 reactive refs and 21
module-level mutable variables — narration flags, echo timestamps, abort
controllers, watchdog timers, a session counter to detect stale callback
chains — each of which existed to keep two flags from disagreeing. A page reload
lost the run. Nothing in it could be tested without a browser.

## Decision

The run is a state machine in `application` on the server, with five states:

`idle → narrating → listening → answering → resuming`

The server decides every transition. The browser does two things: it renders the
events the server sends, and it delivers what the audience said. It holds no
presentation state of its own and makes no decision about what happens next.

One WebSocket carries one run, with typed events in both directions. A message
is a named event with a payload, never an ad-hoc JSON shape agreed between two
call sites.

### What the browser sends, by phase

The browser's input is not one thing. Until the local speech-to-text adapter of
[ADR 0004](0004-provider-neutral-speech.md) exists, the M2 bootstrap uses the
browser's own speech recognition and sends **transcripts**. From M3 it sends
**PCM audio** and the server transcribes. These are two different wire events on
the same socket, not one event whose payload changes meaning, and the run knows
which one it is listening for.

### The latency budget

Narration for slide n+1 is generated while slide n is still being spoken, so on
a prefetch hit the next clip is already waiting when the slide turns and first
audio is bounded by playback start alone. That is the path the 1 s target in
[VISION.md](../VISION.md) describes.

Three paths miss that prefetch, and each is budgeted rather than assumed away:

- **Cold start.** Slide 1 has no predecessor, so its narration is prefetched
  when the run starts — while the operator is still opening the projector
  window — not when he presses next.
- **Skip or jump.** A named miss. Its cost is the model's first token, plus one
  trailing token for the sentence splitter of
  [ADR 0009](0009-german-speech-text.md), plus the first chunk from the voice,
  plus the tunnel, plus the client's jitter buffer
  ([ADR 0007](0007-browser-client-behind-tunnel.md)). It is not covered by the
  1 s target, and the operator sees a thinking state rather than silence.
- **An answer.** No prefetch is possible: the question is not known in advance.
  The same chain runs, which is why the answer target is 3 s and not 1 s.

Until M3 the voice is edge-tts, a network call from the home server
([ADR 0004](0004-provider-neutral-speech.md)), so every one of these budgets is
provisional until measured on the real machine.

Owning the loop does not mean owning what the loop is made of. Two pieces are
taken from [Pipecat](https://github.com/pipecat-ai/pipecat) (BSD-2-Clause) as
components rather than as a framework: [Silero VAD](https://github.com/snakers4/silero-vad)
for voice activity and [smart-turn v3](https://github.com/pipecat-ai/smart-turn)
(BSD-2-Clause) for endpointing. Two of its designs are copied rather than
imported: sentence aggregation as a replaceable text aggregator, and
interruption as "drop the queued speech frames" rather than as a flag.

## Consequences

- The presentation loop is testable without a browser: drive the state machine
  with events and assert the events it emits.
- A page reload rejoins the run instead of ending it, because the run never
  lived in the page.
- Prefetching spends model tokens and time on a slide that the operator may skip
  past. That is the accepted cost of the latency target.
- The browser has two input modes across the phases, so the wire schema carries
  both events from the start and the M3 work is an adapter, not a protocol
  change.
- The server holds a live session per run. The WebSocket connection and the run
  state must not be the same object, or a dropped connection kills the talk.
- Turn-taking, interruption, and endpointing are ours to get right. That is real
  work a framework would have done, and it is the price of the decision below.

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
- **[Pipecat](https://github.com/pipecat-ai/pipecat) as the pipeline owner.**
  This is the one honest counter-argument, and it is rejected for three
  reasons, not for taste. The framework owns the loop and the LLM context, so
  our state machine would become a set of its frame processors. Its frames
  would then be the currency of `application`, which
  [ADR 0001](0001-enforced-layers.md) forbids outside `adapters`. And
  `agent_providers` ([ADR 0003](0003-libraries-for-models-and-auth.md)) would be
  wrapped as a second-class LLM service, leaving two owners of one conversation
  context. Its browser transport pushes the same way: Pipecat's own
  documentation says the FastAPI WebSocket transport suits telephony and
  server-side integrations and steers browser clients to WebRTC, which
  [ADR 0007](0007-browser-client-behind-tunnel.md) cannot use through the
  tunnel.
- **[LiveKit Agents](https://github.com/livekit/agents) as the pipeline owner.**
  The same three reasons, plus a LiveKit media server process and a WebRTC-only
  media path — new infrastructure against
  [ADR 0006](0006-sqlite-and-files.md).
- **[Vocode](https://github.com/vocodedev/vocode-core)** — unmaintained; last
  push 2024-11-15.
- **[TEN Framework](https://github.com/TEN-framework/ten-framework)** — excluded
  on licence: Apache-2.0 with Agora's additional conditions, which forbid
  deployment competing with Agora's offerings and hosting on end-user devices.

## Revisit trigger

At M2, when turn-taking is the actual problem rather than an anticipated one.
Adopting Pipecat wholesale is a legitimate reading of "do not build it twice"
and would delete a lot of code; it costs this record, plus a WebRTC transport
that has to be made to work through the tunnel. That trade is worth re-asking
once, with M2's measurements in hand, and not before.
