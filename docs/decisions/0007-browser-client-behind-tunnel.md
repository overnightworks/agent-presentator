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
- Evidence: the build-vs-reuse survey of 2026-09-06, which established the two
  transport facts below

## Context

The operator presents from a company laptop he does not administer. He can open
a browser. He cannot install a client, open a port, or reach his home network
directly. The deck runs on the home server behind that network.

A talk has no second attempt. Whatever the conference network does, the slides
have to appear.

Two facts about this deployment were established by the 2026-09-06 survey and
decide more than they look like. A Cloudflare tunnel proxies HTTP and WebSocket
over TCP; UDP is not available on a public tunnel hostname, so WebRTC needs a
TURN server or an SFU — new infrastructure against
[ADR 0006](0006-sqlite-and-files.md). And Slidev's own presenter/projector sync
falls back to `BroadcastChannel` when there is no dev server, which is exactly
the static build of [ADR 0005](0005-deck-folder-and-slidev.md);
`BroadcastChannel` is same-browser, same-origin, so a built deck does not sync
across machines by itself.

## Decision

The browser is the whole client. There is no desktop app, no installed helper,
and no local agent on the presenting machine.

Access goes through a Cloudflare tunnel with Cloudflare Access in front of it,
so the home server is reachable without an inbound port, and only by the
operator.

Every deck has two offline fallbacks, produced with its build: a static build
that presents without the server, and a PDF export. A dead tunnel costs the AI
co-presenter, never the talk.

Audio streams as raw Int16 PCM over the per-run WebSocket of
[ADR 0002](0002-server-owned-run.md). There is no second media transport and no
codec: MP3 in Media Source Extensions is not portable — Firefox refuses it —
WebM/Opus would buy an encoder and a muxer for bandwidth this stream does not
need, and PCM sidesteps the whole codec-support matrix. Ordering "slide changed" against "audio chunk n"
is free, because both arrive on the same socket.

That socket must survive a talk, and an idle one does not survive by itself:
`cloudflared` reaps an idle WebSocket in about a hundred seconds, and QUIC drops
idle connections too. Application-level ping and pong on the run socket is
therefore part of this decision, not later hardening. When the socket dies
anyway — conference WiFi, a laptop sleeping — the client reconnects and resumes
the run, which is possible because [ADR 0002](0002-server-owned-run.md) keeps
the run and the connection as separate objects.

Presenter and projector sync across machines comes from that same socket.
Slidev exposes `addSyncMethod` as public API, so our addon registers the run
socket as a sync transport; no fork, no patch, and the static build syncs where
`BroadcastChannel` cannot.

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
- PCM costs bandwidth an encoder would save — mono 16-bit at 16–24 kHz is
  roughly 32–48 KB/s — and buys a client with no decoder state machine in it.
- The client owns a small amount of real audio machinery: an AudioWorklet
  player with a jitter buffer, gapless chunk scheduling, and an interrupt that
  both stops playback and tells the server to stop emitting.
- Keepalive and resume are in the first slice that opens the socket. A run that
  cannot be rejoined is a talk that ends when the network hiccups.
- Cloudflare's plan limits on streaming through a public hostname are an
  operations item to check before the first talk. Nothing here has measured
  them, and a limit found on stage is found too late.

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
- **WebRTC for the audio path.** It is the obvious answer and the tunnel
  forbids it without TURN or an SFU. Its real advantage is loss concealment on
  a lossy link, which a one-way stream to one listener over TCP does not need.
- **An encoded audio stream over MSE.** MP3 there is not portable, and
  WebM/Opus adds an encoder, a muxer, and client state to save bandwidth that
  is not scarce.
- **Slidev's built-in sync.** It is right for a dev server and degrades to
  same-browser only in the static build, which is the mode the talk runs in.
