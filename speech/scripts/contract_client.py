"""Drive the three speech endpoints over the wire.

This process must not import the `speech` package. It is the parallel-lane
proof: a caller that only knows the HTTP contract.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from array import array

import websockets

SENTENCE = (
    "Die Kamera zeigt den Vortragenden, während im Hintergrund "
    "die nächste Folie automatisch vorbereitet wird."
)
SECOND = "Die nächste Folie bitte."
FRAME_SECONDS = 0.1
SILENCE_SECONDS = 0.9


def _must_not_have_imported_speech() -> None:
    if "speech" in sys.modules:
        message = "contract client imported speech"
        raise SystemExit(message)


def _get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _pcm_from_wav(data: bytes) -> tuple[int, bytes]:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        message = "speak did not return a WAV"
        raise SystemExit(message)
    rate = int.from_bytes(data[24:28], "little")
    position = 12
    while position + 8 <= len(data):
        chunk_id = data[position : position + 4]
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        payload = position + 8
        if chunk_id == b"data":
            return rate, data[payload:]
        position = payload + size
    message = "WAV has no data chunk"
    raise SystemExit(message)


def _resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    n_src = len(samples)
    n_dst = max(1, int(n_src * dst_rate / src_rate))
    out = array("h", [0] * n_dst)
    last = max(n_src - 1, 1)
    for index in range(n_dst):
        position = index * last / max(n_dst - 1, 1)
        left = int(position)
        frac = position - left
        right = min(left + 1, n_src - 1)
        value = samples[left] * (1 - frac) + samples[right] * frac
        out[index] = int(max(-32768, min(32767, value)))
    return out.tobytes()


def _speak(base: str, text: str) -> bytes:
    request = urllib.request.Request(
        f"{base}/speak",
        data=json.dumps({"text": text, "language": "de"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    total = sum(sample * sample for sample in samples)
    return (total / len(samples)) ** 0.5 / 32768.0


async def _drain(
    websocket: websockets.ClientConnection,
    events: list[dict[str, object]],
    timeout: float,
) -> None:
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    except TimeoutError:
        return
    if isinstance(raw, str):
        events.append(json.loads(raw))


async def _send_pcm(
    websocket: websockets.ClientConnection,
    pcm: bytes,
    sample_rate: int,
    events: list[dict[str, object]],
) -> int:
    frame_bytes = int(FRAME_SECONDS * sample_rate * 2)
    frames_sent = 0
    for offset in range(0, len(pcm), frame_bytes):
        await websocket.send(pcm[offset : offset + frame_bytes])
        frames_sent += 1
        await _drain(websocket, events, timeout=0.02)
        await asyncio.sleep(FRAME_SECONDS)
    return frames_sent


async def _hear_utterance(
    websocket: websockets.ClientConnection,
    pcm: bytes,
    sample_rate: int,
) -> tuple[list[dict[str, object]], int]:
    events: list[dict[str, object]] = []
    frames_when_first_partial = 0
    frames_sent = 0
    frame_bytes = int(FRAME_SECONDS * sample_rate * 2)
    for offset in range(0, len(pcm), frame_bytes):
        await websocket.send(pcm[offset : offset + frame_bytes])
        frames_sent += 1
        before = len(events)
        await _drain(websocket, events, timeout=0.02)
        if before == 0 and events and frames_when_first_partial == 0:
            frames_when_first_partial = frames_sent
        await asyncio.sleep(FRAME_SECONDS)
    silence = bytes(int(SILENCE_SECONDS * sample_rate * 2))
    await _send_pcm(websocket, silence, sample_rate, events)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if any(event.get("final") is True for event in events):
            break
        await _drain(websocket, events, timeout=0.3)
    return events, frames_when_first_partial


async def _run(base: str) -> None:
    health = _get_json(f"{base}/health")
    print("contract_health", json.dumps(health, sort_keys=True))
    if not health["speaking"]["ready"] or not health["hearing"]["ready"]:
        message = "models are not ready"
        raise SystemExit(message)
    sample_rate = int(health["sample_rate"])

    wav = _speak(base, SENTENCE)
    rate, pcm = _pcm_from_wav(wav)
    pcm = _resample(pcm, rate, sample_rate)
    print(
        "contract_speak",
        f"bytes={len(wav)} pcm={len(pcm)} "
        f"rate={rate}->{sample_rate} rms={_rms(pcm):.4f}",
    )
    if _rms(pcm) < 0.01:
        message = "speak returned silence"
        raise SystemExit(message)

    hear_url = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    hear_url = f"{hear_url}/hear?language=de"
    async with websockets.connect(hear_url, max_size=None) as socket:
        first, partial_at_frame = await _hear_utterance(socket, pcm, sample_rate)
        print(
            "contract_hear_first",
            f"partial_while_sending_at_frame={partial_at_frame}",
            json.dumps(first, ensure_ascii=False),
        )
        second_wav = _speak(base, SECOND)
        rate2, pcm2 = _pcm_from_wav(second_wav)
        pcm2 = _resample(pcm2, rate2, sample_rate)
        second, _partial2 = await _hear_utterance(socket, pcm2, sample_rate)
        print("contract_hear_second", json.dumps(second, ensure_ascii=False))


def main() -> None:
    """Drive health, speak, and hear against BASE_URL."""
    _must_not_have_imported_speech()
    if len(sys.argv) != 2:
        raise SystemExit("usage: contract_client.py http://127.0.0.1:PORT")
    asyncio.run(_run(sys.argv[1].rstrip("/")))


if __name__ == "__main__":
    main()
