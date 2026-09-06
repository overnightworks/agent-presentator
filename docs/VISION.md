# Why this tool exists

Audience: anyone — human or agent — who needs to know what agent-presentator is
for before touching it. This file owns the operator's intent. It owns no
technical choice; those live in [decisions/README.md](decisions/README.md).

## The wish

The operator gives a talk from his company laptop with nothing but a browser.
The Slidev deck lives on his home server. Per talk he can switch on an AI
co-presenter — Claude, Codex, or Grok, through his own subscription — which
speaks the slides with a streamed voice, navigates on its own, listens to the
audience, and answers their questions.

Everything works without the AI too. A static build plus a PDF export survives
a dead tunnel.

## Why now

The first talk, in March 2026, proved the idea works on a stage. Its code did
not survive the proof: it was welded to that one talk. This repository is the
rebuild as a reusable tool, decided by the operator on 2026-09-06.

## How the operator will notice it exists

He drops a new deck folder into Git, logs in from the laptop, presses start,
and Claude presents.

## Phases

A target picture, not a status. What exists today is
[PRODUCT.md](PRODUCT.md).

| Phase | Reached when |
| --- | --- |
| M0 Skeleton | He logs in, presents a deck without AI, and loads the PDF. |
| M1 Narration | The AI presents one deck end to end. |
| M2 Audience | An interjected question is heard by voice, answered, and the presentation continues. |
| M3 GPU speech | Text-to-speech and speech-to-text run locally. |
| M4 Tools | Optional plugin tools. |

## Latency targets

- First audio output after a slide change: under 1 s.
- First audio output of an answer: under 3 s.
- Partial speech-to-text transcript: under 0.5 s.
