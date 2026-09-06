# ADR 0009: German sentence boundaries come from a library; the incremental buffer is ours

Audience: humans and agents turning a model's token stream into text a voice can
speak.

- Status: ACCEPTED 2026-09-06 — needed from phase M1 of [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Evidence: the build-vs-reuse survey of 2026-09-06, which surveyed ten sentence
  splitters and the German normalization ecosystem
- Neighbours: [ADR 0004](0004-provider-neutral-speech.md) owns the voice this
  text is spoken by; [ADR 0002](0002-server-owned-run.md) owns the aggregation
  design this implements

## Context

The model streams tokens; the voice needs whole sentences. A sentence handed
over too early is spoken with the wrong intonation, and one handed over too late
costs the latency target in [VISION.md](../VISION.md). Getting this wrong in
German is easy: `Dr.`, `z. B.`, `bzw.`, and an ordinal like `3.` all look like a
sentence ending.

The survey's decisive finding is that no library does the incremental part. Ten
splitters were checked, and every one of them assumes a mostly complete text.
The buffering is universally the caller's job — LiveKit's buffered sentence
stream and Pipecat's text aggregator are the two reference shapes, and neither
is German-aware.

Normalization is a separate, still-necessary job: a model writes `1.500 €`,
`14.03.`, and Markdown emphasis, and a text-to-speech engine reading those
literally is worse than one reading nothing.

## Decision

Boundaries come from [syntok](https://github.com/fnl/syntok) (MIT): pure Python,
deterministic, no model weights, with a German abbreviation table this
repository extends rather than replaces. [SoMaJo](https://github.com/tsproisl/SoMaJo)
is the swap-in behind the same seam if measurement demands it, and its GPL-3.0
licence is the reason it is second rather than first.

The incremental aggregator is ours, roughly forty lines: accumulate the token
deltas, re-run the boundary function over the buffer tail, and emit a sentence
only once trailing content follows it — so a buffer ending in `Dr.` never fires.

Normalization before the voice is a small pipeline of maintained parts:

- [german_transliterate](https://github.com/repodiac/german_transliterate) for
  German numbers, dates, currency, ordinals, and abbreviations. Its licence is
  CC-BY-4.0, which is unusual for code and is checked before it ships.
- `num2words` for cardinals and ordinals.
- `emoji` to strip what a voice cannot read.
- `markdown-it-py`'s tokenizer with our own plain-text renderer, so emphasis and
  code fences do not reach the voice as characters.

## Consequences

- One owner decides where a sentence ends, and it is testable directly: feed a
  token sequence, assert the emitted sentences.
- The abbreviation table is a place this product's German has to be maintained
  by hand. That is cheap and visible, which is why it is preferred to a model.
- Emitting only on trailing content adds one token's worth of delay per
  sentence. That is the deliberate price of never speaking half a sentence.
- Four small dependencies carry the normalization. Each is replaceable behind
  the same function, and `german_transliterate` has a single maintainer, which
  is a named risk rather than a hidden one.

## Rejected alternatives

- **A library for the incremental part.** There is none. Adopting LiveKit's or
  Pipecat's aggregator means adopting their framework
  ([ADR 0002](0002-server-owned-run.md)) for forty lines.
- **pysbd and blingfire.** pysbd's last release is from 2021 and blingfire is
  archived; the survey also measured blingfire at 75 % boundary accuracy against
  pysbd's 97.9 %.
- **wtpsplit, stanza, and spaCy.** Transformer inference or a full NLP pipeline
  per call, in the code path that must produce first audio in under a second.
- **unidecode and text-unidecode.** They strip umlauts and ß, which destroys
  German pronunciation — the opposite of what a normalizer is for.
- **NeMo-text-processing.** It has a real German grammar, but it pulls in
  pynini and OpenFst. It stays a fallback if the small pipeline proves
  insufficient.
- **Letting the text-to-speech engine normalize.** Piper's and espeak-ng's
  number reading is documented as unreliable, and the German community builds
  around them all added a normalization layer of their own.
