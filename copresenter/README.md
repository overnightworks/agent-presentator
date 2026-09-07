Audience: the operator running this beside an instance, or an agent proving it.

# The co-presenter

A voice that knows the deck. It is not the product. It lives beside the
instance so a demo cannot break the lobby, the gates, or `src/presentator`.

The overlay rides in the deck (`examples/copresenter-deck`). The service reads
that deck's `slides.md`, asks Claude, and speaks through the local speech
service from [#74](https://github.com/overnightworks/agent-presentator/issues/74).

## What it is

Three things the March tool did not do:

1. **It knows the deck.** There is no hand-typed slide list and no database. The
   process is pointed at a deck directory and reads the slides and speaker
   notes from the markdown.
2. **It streams.** Claude's text is split into sentences as it arrives. Each
   finished sentence is `POST /speak` on the speech service immediately, so the
   first words are spoken while the rest is still being written.
3. **It says what it is.** `GET /who` names the answering model and the speech
   models behind it, read from the speech service's `GET /health`.

Speech goes through one boundary in this code (`speak`, `hear`). The default
implementation is the local service at `COPRESENTER_SPEECH_URL`. This process
never imports that service and never loads a model. The overlay's hearing
toggle uses that same local socket first; the browser's own `SpeechRecognition`
is the fallback so a missing speech service does not leave the demo deaf. The
fallback sends audio to whatever that browser uses (Chrome: a remote service)
and must be named on stage.

## What it sends where

```
mic  --PCM-->  overlay  --WS /hear-->  copresenter  --WS /hear-->  speech service
                 |                          |
                 |  POST /ask {said,slide}  |
                 +--------------------------+
                            |
                     Claude (agent-providers)
                            |
                     each sentence --POST /speak--> speech service --WAV--> overlay
```

| From | To | What |
| --- | --- | --- |
| Overlay | `GET /who` | Which model answers, which speech models, sample rate |
| Overlay | `WS /hear?language=de` | Raw 16-bit PCM frames from the microphone |
| Copresenter | Speech `WS /hear?language=de` | Those frames, forwarded |
| Speech | Overlay (via copresenter) | `{"text","final"}` partials and finals |
| Overlay | `POST /ask` | `{said, slide, language}` |
| Copresenter | Claude | Current slide + the deck around it + what was said |
| Copresenter | Speech `POST /speak` | One finished sentence, `{text, language}` |
| Copresenter | Overlay | SSE: `text`, `sentence`, `audio` (WAV as `wav_b64`), `done` |

The overlay is off until the Presenter switch is turned on. Off, it neither
listens nor speaks and the microphone is released.

## How to run it with the speech service

The provider key is `ANTHROPIC_API_KEY` in the environment. The process
refuses to start without it. The key is never logged and never written to a
file.

```sh
export ANTHROPIC_API_KEY="…"          # required
export COPRESENTER_SPEECH_URL="http://127.0.0.1:8765"   # stand-in; #74's own default is :8090
export COPRESENTER_DECK="../examples/copresenter-deck"
export COPRESENTER_CLAUDE_MODEL="claude-sonnet-4-6"   # optional
```

Speech service (the real one from #74, or the stand-in while that lane is still
building):

```sh
# real:
#   follow speech/README.md, then point COPRESENTER_SPEECH_URL at it
# stand-in:
uv run copresenter-standin
```

This service:

```sh
uv run copresenter
```

The deck, from `frontend/` after `pnpm install --frozen-lockfile`:

```sh
pnpm exec slidev ../examples/copresenter-deck/slides.md
```

Open the talk and turn **Presenter** on. A different service address is
`?copresenter=http://127.0.0.1:3040` on the talk URL.

Copy `global-bottom.vue` and `components/CoPresenter.vue` into any other deck
folder to take the overlay with you.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | (none) | Claude credential. Required. Not a `COPRESENTER_*` field. |
| `COPRESENTER_HOST` | `127.0.0.1` | Bind address |
| `COPRESENTER_PORT` | `3040` | Bind port |
| `COPRESENTER_SPEECH_URL` | `http://127.0.0.1:8765` | Local speech service |
| `COPRESENTER_DECK` | `examples/copresenter-deck` | Folder with `slides.md` |
| `COPRESENTER_CLAUDE_MODEL` | `claude-sonnet-4-6` | Model name given to agent-providers |
| `COPRESENTER_LANGUAGE` | `de` | Language sent to `/speak` and `/hear` unless the overlay overrides it |

## What it does not yet do

- Navigate the deck, click animations, or take over the talk.
- Classify intent, use a wake word, or talk over itself (playback gates new
  questions; a late transcript that matches what was just spoken is dropped).
- Parse Slidev as Slidev does: this reader splits on `---` and takes the last
  HTML comment as notes. Imported slides and click steps are unseen.
- Split abbreviations such as `z.B.` correctly.
- Search a knowledge graph, the web, or anything outside the deck folder.
- Authenticate callers. It is a local demo process.
- Live inside the instance. Productising it is a later milestone.
- Guarantee the real GPU speech service. If that process is not ready, run the
  stand-in and say so. `GET /who` reports which speech models answered.

The stand-in is not a voice. It returns a short tone and one canned German
transcript so the loop can be proven against the contract.
