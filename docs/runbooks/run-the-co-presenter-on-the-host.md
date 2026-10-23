# Runbook: run the speech service and the co-presenter on the host

Reader: the operator of the machine that holds the GPU.

The voice that answers beside a talk is two host processes: the speech service
on `127.0.0.1:8090` (`speech/`, never exposed) and the co-presenter
(`copresenter/`). Who may use them and what crosses the boundary is
[co-presenter and voice](../reference/co-presenter-and-voice.md); the authority
they share is [trust boundaries](../explanation/trust-boundaries.md).

## Before you start

- `loginctl show-user <user> -p Linger` says `Linger=yes`, so the user units
  outlive the shell that started them.
- The shared Claude token exists ([set it](set-the-claude-token.md)), and the
  `claude` executable is on `COPRESENTER_CLI_SEARCH_PATH`.
- The speech service's `speech.sock` serves `/health` and `/hear` before
  Presentator is upgraded.
- Both host processes and the Presentator container use the same positive
  non-root `PRESENTATOR_RUNTIME_UID`, and `PRESENTATOR_TALK_ORIGIN` is set for
  Compose ([start an instance](start-an-instance-in-a-container.md)).
- No file under `/etc/claude-code/` on this host may configure an export: the
  Datenschutz says the CLI's own telemetry and nonessential traffic are off,
  and the child's switches do not override a managed export.
- The Chatterbox provider is a separate closed virtual environment under
  `SPEECH_PROVIDER_ROOT/chatterbox`; speech executes its
  `.venv/bin/presentator-chatterbox-worker` directly, never through PATH.

## Steps

Run each as a `systemctl --user` unit, speech first; it is ready when
`GET /health` says both models are:

```sh
# speech/, then copresenter/, each as a user unit with these values
SPEECH_HOST=127.0.0.1 SPEECH_PORT=8090 uv run presentator-speech
COPRESENTER_TRANSPORT=unix \
PRESENTATOR_RUNTIME_UID=<shared uid> \
COPRESENTER_SPEECH_URL=http://127.0.0.1:8090 \
COPRESENTER_DECK=<the deck folder> \
  uv run --no-sync --frozen copresenter
```

`--no-sync --frozen` runs the environment the last `uv sync` installed instead
of re-resolving `overnightworks-agent-providers` at every start, which a
reboot without network access would otherwise fail at with exit 2.

Speech owns `SPEECH_STATE_DIRECTORY` (by default
`~/.local/state/presentator-speech`) as a mode-0700 directory. Its mode-0600
`selected-voice` file records the downloaded baseline chosen through
Settings · Voice, so keep it with the host state when moving an instance.

## Check

After an upgrade, restart `presentator-copresenter.service`, then ask one
question on stage to see the answer arrive.

Related: [clear leftover turn homes](clear-leftover-turn-homes.md).
