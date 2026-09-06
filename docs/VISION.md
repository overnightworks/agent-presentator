# Why this tool exists

Audience: anyone — human or agent — who needs to know what agent-presentator is
for before touching it. This file owns the operator's intent. It owns no
technical choice; those live in [decisions/README.md](decisions/README.md).

## The wish

The operator gives a talk from his company laptop with nothing but a browser.
The Slidev deck lives on his home server. Per talk he can switch on an AI
co-presenter — Claude, Codex, or Grok, through the instance host's own
subscription — which speaks the slides with a streamed voice, navigates on its
own, listens to the audience, and answers their questions.

Everything works without the AI too. A static build plus a PDF export survives
a dead tunnel.

The operator is the person this is built for, and the tool is an instance with
accounts rather than a program for one person: an admin creates users, decks and
sources have owners, and someone else can host their own instance. What that
means in detail is [ADR 0011](decisions/0011-instance-users.md).

How it looks and which language it speaks are configuration, not code: one theme
file and one message catalog, so a different look or a second language is a file
rather than a change ([ADR 0012](decisions/0012-themes-and-language.md)).

## Why now

The first talk, in March 2026, proved the idea works on a stage. Its code did
not survive the proof: it was welded to that one talk. This repository is the
rebuild as a reusable tool, decided by the operator on 2026-09-06.

## How the operator will notice it exists

He pushes a new deck folder to a private git repository, logs in from the
laptop, presses start, and Claude presents.

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

- First audio output after a slide change: under 1 s, on a prefetch hit. A jump
  and a cold start are budgeted exceptions, not covered by this number; the
  budgets are in [ADR 0002](decisions/0002-server-owned-run.md). The head
  flagged this qualifier to the operator on 06.09.2026 rather than quietly
  redefining the target.
- First audio output of an answer: under 3 s.
- Partial speech-to-text transcript: under 0.5 s.

The build-vs-reuse survey of 06.09.2026 found the 0.5 s partial-transcript
target at risk — no maintained German speech-to-text server publishes a figure
below it — and it awaits an operator ruling: soften it to 1 s for M2 with 0.5 s
as the M3 goal, or pull the Voxtral spike of
[ADR 0004](decisions/0004-provider-neutral-speech.md) earlier. The target stands
as written until he rules.
