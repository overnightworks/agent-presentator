# Co-presenter and voice reference

Audience: an engineer or operator looking up what answers beside a talk, who
may use it, and what bounds it. Running it is
[run the speech service and the co-presenter](../runbooks/run-the-co-presenter-on-the-host.md);
the authority it shares is [trust boundaries](../explanation/trust-boundaries.md).

## Processes

Two host processes sit beside Presentator. The speech service
([`speech/`](../../speech/README.md)) holds a local speaking model and a local
hearing model, listens on `127.0.0.1:8090`, never exposed, and offers hearing
and its readiness to Presentator directly through its private same-UID Unix
socket `speech.sock` (`/health`, `/hear`); against an older speech,
`/copresenter/who` answers 503 and hearing reports its failure. The
co-presenter (`copresenter/`) only answers: it asks Claude through the `claude`
executable on `COPRESENTER_CLI_SEARCH_PATH` with the shared setup token, and is
private to Presentator through its own same-UID Unix socket. Which speech
models run and what they cost on the card is owned by the speech README.

## Routes and access

The repository overlay calls relative `/copresenter/who`, `/copresenter/ask`
and `/copresenter/hear` routes. The talk host serves them under its own session
and CSRF cookie and its own Origin; the lobby answers 404 on them. Only the
admin role may use them, through the existing active session: a signed-in
person without it gets 403 on `/who` and `/ask` and a refused handshake (HTTP 403, before accept) on
`/hear`; an accepted hearing ends with a 1008 close when the session or the role
goes. Presentator checks the session and, for the WebSocket, the exact
Origin before opening the socket.
`/ask` also carries the session's CSRF cookie (`__Host-csrf_token`) in
`x-csrf-token`. Logout, expiry, loss of the account row, loss of the admin role
or a session-store failure closes the hearing lease and an ongoing answer
within one second, since Presentator rechecks the answer and hearing lease
every half second; generation, capture and queued or playing audio stop.

## Bounds on `/ask`

`said` is at most 2000 characters and `language` is empty or `de`/`en`;
anything else is refused with 422 before a turn starts. A session streams one
answer at a time and may ask 20 questions per 10 minutes. An ask beyond either
limit gets 429 with a `Retry-After` header (seconds until it may be asked
again) and starts no turn. `deck` names the asker's deck and is at most 255
characters, else 422; any deck the asker does not own built (malformed, missing,
nonexistent, unbuilt, source-less or another admin's) gets one identical 404
before the limiter, so it spends no question.

## Turn homes

Each turn's home lives below `COPRESENTER_CLI_WORKING_ROOT`; the service tries
to remove it when the turn ends ([clear leftovers](../runbooks/clear-leftover-turn-homes.md)).

## Settings · Voice

An authenticated admin can open Settings · Voice; a non-admin receives 403
before the private service is called. The surface reads one private, local
status snapshot and shows Piper, Chatterbox, Qwen3-TTS 0.6B, VoxCPM2 and NVIDIA
Magpie in that order. This describes source capability, not an inventory of any
running instance.

- Downloaded Piper or Chatterbox, and Qwen, VoxCPM2 or Magpie with their exact
  local snapshots and executables, and a voice whose previous Load failed have
  one Load action. A usable Qwen worker with an incomplete pinned snapshot has
  explicit Download, which preserves selection and partials while the
  catalogue polls local state.
- The private speech service synchronously makes the choice active and durable
  while current captured speech can finish. Speech owns
  `SPEECH_STATE_DIRECTORY` (by default `~/.local/state/presentator-speech`) as
  a mode-0700 directory; its mode-0600 `selected-voice` file records the
  downloaded baseline chosen here.
- A missing or malformed private response hides all rows and offers Check
  again.
- One verified active row also names a local player: its DE or EN fixed sample
  waits while current speech owns synthesis, then plays through that exact
  active voice.

## Voices

Speech runs selected Chatterbox, Qwen, VoxCPM2 and Magpie voices through one
isolated-provider process owner and one binary worker loop; each provider has
its own locked SDK environment. The Chatterbox provider is a separate closed
virtual environment under `SPEECH_PROVIDER_ROOT/chatterbox`, whose
`.venv/bin/presentator-chatterbox-worker` speech executes directly and never
resolves through PATH. An admitted response keeps its captured voice until it
closes; switching then reaps a deselected provider process before Load
succeeds, while Piper stays resident.

- Qwen speaks German or English through its configured built-in preset and
  returns a completed 24 kHz waveform in bounded PCM frames.
- VoxCPM2 streams text-only German or English at 48 kHz from its exact cached
  snapshot.
- Magpie restores its exact cached model and NanoCodec archives with one
  configured fixed voice and produces completed 22,050 Hz WAV speech.
