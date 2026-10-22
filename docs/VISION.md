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

The tool is an instance with accounts rather than a program for one person: an
admin creates users, decks and sources have owners, and someone else can host
their own instance ([ADR 0011](decisions/0011-instance-users.md)). How it looks
and which language it speaks are configuration, not code
([ADR 0012](decisions/0012-themes-and-language.md)).

## How the operator will notice it exists

He pushes a new deck folder to a private git repository, logs in from the
laptop, presses start, and Claude presents.

## Latency targets

- First audio after a slide change: under 1 s on a prefetch hit; a jump and a
  cold start are budgeted exceptions ([ADR 0002](decisions/0002-server-owned-run.md)).
- First audio of an answer: under 3 s.
- Partial speech-to-text transcript: under 0.5 s.
