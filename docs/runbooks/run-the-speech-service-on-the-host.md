# Runbook: run the speech service on the host

Reader: the operator of the machine that holds the GPU.

The voice that answers beside a talk is the speech service (`speech/`), a host
process on `127.0.0.1:8090` that is never exposed and is reached through
`speech.sock`. Who may use it and what crosses the boundary is
[co-presenter and voice](../reference/co-presenter-and-voice.md); the authority
it shares is [trust boundaries](../explanation/trust-boundaries.md).

## Before you start

- `loginctl show-user <user> -p Linger` says `Linger=yes`, so the user unit
  outlives the shell that started it.
- Speech and the Presentator container use the same positive non-root
  `PRESENTATOR_RUNTIME_UID`, and `PRESENTATOR_TALK_ORIGIN` is set for Compose
  ([start an instance](start-an-instance-in-a-container.md)).
- The Chatterbox provider is a separate closed virtual environment under
  `SPEECH_PROVIDER_ROOT/chatterbox`; speech executes its
  `.venv/bin/presentator-chatterbox-worker` directly, never through PATH.

## Steps

Run speech as a `systemctl --user` unit; it is ready when `GET /health` says
both models are:

```sh
SPEECH_HOST=127.0.0.1 SPEECH_PORT=8090 uv run presentator-speech
```

Speech owns `SPEECH_STATE_DIRECTORY` (by default
`~/.local/state/presentator-speech`) as a mode-0700 directory. Its mode-0600
`selected-voice` file records the downloaded baseline chosen through
Settings · Voice, so keep it with the host state when moving an instance.

## Check

`speech.sock` serves `/health` and `/hear`. On stage, ask one question and see
the answer arrive.
