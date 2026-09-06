# ADR 0007: The client is the browser alone, reached through a Cloudflare tunnel, with a static build as fallback

Audience: humans and agents working on the client surface or on how a talk is
reached from outside.

- Status: ACCEPTED 2026-09-06 — logging in and presenting without AI are phase
  M0 of [VISION.md](../VISION.md)
- Date: 2026-09-06
- Decision authority: the operator's ruling of 2026-09-06 recorded on
  [#2](https://github.com/overnightworks/agent-presentator/issues/2)
- Neighbours: [ADR 0002](0002-server-owned-run.md) — the browser renders events
  and captures the microphone, nothing else;
  [ADR 0005](0005-deck-folder-and-slidev.md) owns the deck build

## Context

The operator presents from a company laptop he does not administer. He can open
a browser. He cannot install a client, open a port, or reach his home network
directly. The deck runs on the home server behind that network.

A talk has no second attempt. Whatever the conference network does, the slides
have to appear.

## Decision

The browser is the whole client. There is no desktop app, no installed helper,
and no local agent on the presenting machine.

Access goes through a Cloudflare tunnel with Cloudflare Access in front of it,
so the home server is reachable without an inbound port, and only by the
operator.

Every deck has two offline fallbacks, produced with its build: a static build
that presents without the server, and a PDF export. A dead tunnel costs the AI
co-presenter, never the talk.

Audio plays only in the projector window. A second open window stays silent, so
no view can start a competing voice.

## Consequences

- The presenting machine needs nothing but a browser and a login.
- Cloudflare is on the critical path for the live, AI-driven mode. This is the
  reason the fallbacks are part of the build rather than a recovery procedure.
- The fallbacks must be produced ahead of time and carried to the talk to be
  worth anything; a fallback generated on demand shares the failure it exists
  to survive.
- The microphone requires a secure context and an explicit permission grant in
  that browser, which is a step the operator takes before the room fills.
- Which window is the projector window becomes explicit state the client must
  hold, rather than a property of whichever tab spoke last.

## Rejected alternatives

- **A native or installed client.** The company laptop is the constraint that
  defines this product; anything requiring installation cannot be used where the
  talks happen.
- **Exposing the home server directly, by port forwarding or VPN.** An inbound
  port on a home network to serve one person is the larger risk, and a VPN needs
  client software the laptop will not have.
- **A live-only presentation with no static fallback.** The March 2026 talk
  proved the concept works when the network cooperates. A product that only
  works then is not one the operator can rely on for the next talk.
- **Audio in any window.** Two windows speaking over each other is a failure
  that happens in front of the audience and cannot be undone.
