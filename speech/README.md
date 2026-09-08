# Local speech

Audience: the operator starting this process on the home server, and the
co-presenter that will call it.

One process holds a German voice and a German listener resident, and offers
them over HTTP. Nothing in `src/presentator` imports this package. The caller
owns the address. Piper is the default voice until the operator has judged
Chatterbox. Set `SPEECH_SPEAKING_MODEL=ResembleAI/chatterbox` for the streaming
card voice.

## Models

| Role | Hugging Face | Licence | Why this one |
| --- | --- | --- | --- |
| Speaking (default) | [`rhasspy/piper-voices`](https://huggingface.co/rhasspy/piper-voices) voice `de_DE-thorsten-medium` | Voice recordings CC0; ONNX weights under the piper-voices MIT repo. The `piper-tts` runtime is `GPL-3.0-or-later`. The HTTP boundary keeps it out of `src/presentator` while the speech program itself carries the GPL obligations. | German is documented and not in question. #70 measured this voice on this 3090 at 0.23 s to first audio on CPU. `thorsten-high` on the same sentence took 1.21 s through this service, over the one-second first-audio target, so medium is the default. The voice does not compete with the listener for the card. |
| Speaking (optional) | [`ResembleAI/chatterbox`](https://huggingface.co/ResembleAI/chatterbox) Multilingual V3 | MIT (`chatterbox-tts` 0.1.7) | Real German, streams PCM while it synthesises, 24 kHz. Installed `from_pretrained` takes only `device` and loads V2; this service loads `t3_mtl23ls_v3.safetensors` from the local Hub cache. `resemble-perth` 1.0.1 is the package pin; Chatterbox always constructs a watermarker and has no disable flag, so the service installs perth's `DummyWatermarker` (the implicit net needs `pkg_resources`, which this venv does not have). |
| Hearing | [`Systran/faster-whisper-large-v3`](https://huggingface.co/Systran/faster-whisper-large-v3) | MIT (CTranslate2 conversion of [`openai/whisper-large-v3`](https://huggingface.co/openai/whisper-large-v3), also MIT) | Already cached on this machine. German is a documented language. Partials are chunked re-decode of a growing buffer, not a native streaming architecture. |

Checked and not chosen:

- **Kokoro / Kokoro-German** (`Tundragoon/Kokoro-German`, Apache-2.0). Official Kokoro has no documented German. The community German fine-tune warns it is undertrained.
- **XTTS-v2** and maintained forks. Weights under the Coqui Public Model License (non-commercial). ADR 0004 already excluded them.
- **Orpheus** (`Thorsten-Voice/tv-orpheus-v1`, Apache-2.0 on the fine-tune; some GGUF conversions inherit Llama 3.2). A 3B SNAC talker, roughly 8 GB resident. First audio for a short sentence is the LLM's first codec frame, not a VITS chunk; not measured on this card, and it would sit on the GPU beside Whisper.
- **Qwen3-TTS-0.6B** (`Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`, Apache-2.0). The next speaking candidate if Chatterbox's voice does not hold up under the operator's ear; it wants FlashAttention-2.
- **Voxtral Mini 4B Realtime** (Apache-2.0, documented German, native streaming, sub-500 ms in the paper). Not cached here, wants vLLM, and ships no server that matches this contract. ADR 0004 still names it as the M3 spike.

The listener is not a streaming-native model. Kyutai STT has no German checkpoint. Voxtral is the current streaming-native candidate and was left for that spike.

## Card

Piper runs on the CPU. Chatterbox Multilingual V3 and Whisper large-v3 float16
share the 3090 when Chatterbox is selected. Together they fit beside the
display processes.

Measured on this RTX 3090 (24 GB), German sentence of 14 words,
`scripts/prove.py`, 08.09.2026, Piper default:

| What | Number |
| --- | --- |
| Time to first byte of `/speak` (wire, first HTTP body byte) | 0.216 s (`speaking.streams` is `false`; Piper allows the first byte when the sentence is done; limit 0.5 s) |
| Total `/speak` | 0.226 s |
| WAV duration / RMS | 5.515 s / 0.173 (not silence) |
| First partial transcript | 0.968 s (frame 7 of 100 ms frames), while frames were still being sent |
| Final transcript | matched the spoken sentence at 9.354 s |
| Second utterance on the same socket | `"Die nächste Folie bitte."` at 3.027 s, no reload |
| Card before load | 1557 MiB |
| Card with both resident | 5437 MiB |
| Hearing footprint | ~3880 MiB GPU |
| Speaking footprint | 0 MiB GPU (CPU / ONNX) |

Measured again with `SPEECH_SPEAKING_MODEL=ResembleAI/chatterbox`, same
sentence, same card, 08.09.2026. The operator's live speech process was also
on the GPU (~4018 MiB) and was not stopped:

| What | Number |
| --- | --- |
| Time to first byte of `/speak` (wire, German) | 0.516 s (`speaking.streams` is `true`; limit 1.0 s) |
| Time to first byte (English) | 0.495 s |
| Total `/speak` / audio duration / RTF (German) | 5.947 s / 5.120 s / 0.86× |
| WAV sample rate / RMS | 24 000 Hz / 0.128 (not silence) |
| First partial transcript | 0.969 s (frame 7), while frames were still being sent |
| Final transcript | matched the spoken sentence at 8.612 s |
| Second `/speak` while the first streams | both WAVs distinct speech; the voice lock serialises generation |
| Card before this process | 6158 MiB |
| Card with Chatterbox + Whisper resident | 13333 MiB |
| Chatterbox footprint | ~3560 MiB GPU |
| Hearing footprint | ~3620 MiB GPU |

A cold first German request, before CUDA kernels had run, took 1.362 s to the
first byte. Load now synthesises a short German warmup so `ready` means the
first real sentence is in budget. The German WAV from the proof is at
`/tmp/issue-83-voice/german.wav` for the operator to judge.

CTranslate2 does not bundle CUDA. This project installs `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` and preloads them at start.

Weights stay in:

- Piper voices: `~/.cache/piper/`
- Chatterbox: `~/.cache/huggingface/hub/models--ResembleAI--chatterbox` (snapshot `5bb1f6ee`)
- Whisper: `~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3`

## Contract

- `GET /health` → `{"speaking": {"model", "ready", "streams", "sample_rate"}, "hearing": {"model", "ready"}, "sample_rate", "card_memory_mb"}`. Answers while models are still loading, with `ready` false. `speaking.streams` is whether the voice yields PCM while it is still synthesising: `false` for Piper (one completed chunk per sentence, so the whole waveform exists before the first byte leaves), `true` for a model that yields as it goes. `speaking.sample_rate` is the WAV rate.
- `POST /speak` with `{"text", "language"}` → chunked `audio/wav`, 16-bit PCM mono. One sentence per request. The first bytes leave as early as the model allows. The WAV header carries the voice's native rate (22 050 Hz for Thorsten, 24 000 Hz for Chatterbox), also reported as `speaking.sample_rate`.
- `WS /hear?language=de` takes binary frames of raw 16-bit PCM mono at `sample_rate` (16 000 Hz) and sends `{"text", "final"}`. The socket stays open; the model is not reloaded between utterances.

`sample_rate` is the hear rate. Speak is a WAV, so its rate is in the header. A caller that feeds speak output into hear must resample.

## Settings

All `SPEECH_*`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPEECH_HOST` | `127.0.0.1` | Bind address |
| `SPEECH_PORT` | `8090` | Bind port |
| `SPEECH_DEVICE` | `cuda` | Device for the hearing model and for Chatterbox (`cuda` or `cpu`). Piper stays on CPU. |
| `SPEECH_SPEAKING_MODEL` | `de_DE-thorsten-medium` | Piper catalogue name, or `ResembleAI/chatterbox` for Multilingual V3 |
| `SPEECH_HEARING_MODEL` | `Systran/faster-whisper-large-v3` | Hugging Face id or faster-whisper size name |
| `SPEECH_DEBUG` | `false` | When true, logs the text of what was spoken or heard. Audio is never logged. |
| `SPEECH_VOICE_CACHE` | `~/.cache/piper` | Where Piper ONNX files are kept |

If a model cannot be loaded the process exits and names which one failed.

## Run

From this directory:

```sh
uv sync --group dev
uv run presentator-speech
```

Stage proof (takes `/tmp/probe-stack.lock`, checks load, one heavy step at a
time). It selects Chatterbox unless `SPEECH_SPEAKING_MODEL` is already set:

```sh
uv run python scripts/prove.py
```

Checks:

```sh
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
```
