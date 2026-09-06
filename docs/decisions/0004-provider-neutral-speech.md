# ADR 0004: Speech is a port, and the target voice runs locally on the home server GPU

Audience: humans and agents choosing or implementing a voice or a transcriber.

- Status: ACCEPTED 2026-09-06 — the local models are phase M3 of
  [VISION.md](../VISION.md); the bootstrap implementations come earlier
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0003](0003-libraries-for-models-and-auth.md) — the model that
  thinks and the voice that speaks are deliberately separate seams;
  [ADR 0009](0009-german-speech-text.md) owns the text that reaches the voice
- Evidence: the build-vs-reuse survey of 2026-09-06, which produced the filter
  and the candidate backends below

## Context

Claude, Codex, or Grok may present the same deck. If the voice came from the
provider, the talk would sound different depending on which model was available
that day, and the operator's audience would hear the plumbing.

The home server has an RTX 3090. Local text-to-speech and speech-to-text models
that run on it exist, and their quality and latency differ enough that the
choice must be measured rather than argued.

The 2026-09-06 survey applied three filters to the candidates, and they cut
deeper than expected. No research-only or non-commercial weights: that excludes
XTTS-v2 (weights under CPML), F5-TTS (CC-BY-NC) and Fish-Speech / OpenAudio
(Fish Audio Research License). Nothing that wants the whole 24 GB: that excludes
Higgs Audio v2. Nothing without documented, non-experimental German: that
excludes Kokoro — and with it
[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) and
[Speaches](https://github.com/speaches-ai/speaches), the two best-engineered
"just reuse a server" answers, since Speaches hosts Kokoro only. It also
excludes VibeVoice-Realtime, whose own documentation calls its multilingual
support experimental and English-intended.

On the listening side the survey's finding was that most "streaming Whisper" is
chunked re-decoding, not streaming, and no maintained Whisper-family server
publishes a sub-500 ms partial. The two genuinely streaming architectures are
Kyutai STT, which has no German checkpoint, and Voxtral Mini 4B Realtime
(Apache-2.0, documented sub-500 ms, fits a 3090), which ships no
browser-facing server.

## Decision

Text-to-speech and speech-to-text are ports in `ports`, with implementations in
`adapters` ([ADR 0001](0001-enforced-layers.md)).

The target implementation for both is a local model on the home server GPU. The
voice is therefore independent of which model is presenting, and no audio leaves
the machine.

Until then, two bootstrap implementations stand in: edge-tts for the voice and
the browser's own speech recognition for listening. They exist to make phases M1
and M2 reachable, not because they are good enough.

The first backends behind those ports are named, in order:

- Voice: [CosyVoice2](https://github.com/QwenAudio/CosyVoice) (Apache-2.0)
  behind its own streaming server; then
  [Chatterbox Multilingual](https://github.com/resemble-ai/chatterbox) (MIT)
  through a community server; then Piper with the Thorsten voice as the
  low-latency degraded fallback.
- Listening: [RealtimeSTT](https://github.com/KoljaB/RealtimeSTT) (MIT) with the
  faster-whisper engine only. The engine is pinned and CI checks the licences of
  its optional engines, which otherwise pull in Porcupine, Parakeet and
  `kroko_onnx`. Before M3 freezes, a measured spike puts Voxtral Mini 4B
  Realtime on vLLM behind the same port.

Which of these wins is decided in M3 by measuring German quality and
first-chunk latency on this 3090, against the latency targets in
[VISION.md](../VISION.md). It is not decided by reading a claim, because none of
the candidates publishes a German number worth trusting.

**No wake word.** Listening starts from push-to-talk or an explicit listening
state, never from a spoken trigger.

**Echo is handled by knowing what we said, not by cancelling it.** While speech
is playing, transcription is gated; a late transcript that matches the text just
spoken is dropped. The browser's own `echoCancellation` and `noiseSuppression`
stay on as a second layer, and are not relied on: browser AEC subtracts a
loopback of what the browser renders, and the talk's audio comes out of external
room speakers, where that reference no longer matches what the microphone hears.

## Consequences

- A voice change is an adapter swap and a configuration value.
- The presentation loop can be built and tested against a fake speech port
  before any real engine exists.
- The bootstrap path has real limits that must not be mistaken for product
  behavior: edge-tts is a network call to a third party, and browser speech
  recognition is a different engine per browser.
- Running a speech model on the GPU competes with anything else that wants it.
  Whether that is a real conflict is an M3 measurement, not an assumption.
- The licence filter costs the most convenient reuse path. There is no
  ready-made server that speaks good German under a permissive licence, so
  every voice backend arrives with its own server to run.
- Half-duplex gating means the presenter cannot be interrupted mid-word by
  sound alone; the interruption arrives through the listening state, which is
  the operator's to open.

## Rejected alternatives

- **A provider-bound voice, such as Grok TTS** (rejected by the operator,
  2026-09-06). It ties how the talk sounds to which subscription answers,
  exactly the coupling this record exists to prevent, and it re-splits a solved
  problem across three providers.
- **Freezing the winner now.** The order above is where the measurement starts,
  not its result. German quality and first-chunk latency on this one machine
  decide it, and deciding before M3 would be a guess written down as a
  decision.
- **Keeping edge-tts as the target.** It is a third-party network dependency in
  the one code path that must keep working on a stage with a bad conference
  network.
- **A wake word.** Porcupine's engine is proprietary behind an access key
  validated online, and its free-tier keys stop working 2026-06-30 — an offline,
  self-hosted tool cannot depend on that. openWakeWord's code is Apache-2.0 but
  its pre-trained models are CC-BY-NC-SA 4.0 and English-only, so a German
  trigger would mean training our own. The requirement was weak to begin with:
  one operator, with the browser already in front of him.
- **Relying on browser acoustic echo cancellation as the mechanism.** It
  subtracts a loopback of what the browser renders, and the talk's audio leaves
  through external room speakers, so its reference does not match the room. We
  hold strictly better information than any generic canceller anyway: the exact
  text and the exact playback window.
