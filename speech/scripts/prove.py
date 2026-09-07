"""Stage proof: start the service, drive health/speak/hear, then the contract client.

Takes /tmp/probe-stack.lock, checks the 1-minute load, runs one heavy step at a
time, and leaves no process behind. Model weights stay in the Hugging Face and
Piper caches.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

from speech.cuda_libs import cuda_library_dirs

SENTENCE = (
    "Die Kamera zeigt den Vortragenden, während im Hintergrund "
    "die nächste Folie automatisch vorbereitet wird."
)
SECOND = "Die nächste Folie bitte."
LOCK_PATH = Path("/tmp/probe-stack.lock")
LOAD_CEILING = 18.0
READY_TIMEOUT_SECONDS = 180.0
PIPER_CACHE = Path.home() / ".cache" / "piper"
HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"
WHISPER_CACHE = HF_CACHE / "models--Systran--faster-whisper-large-v3"


def _load_1min() -> float:
    return float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])


def _wait_for_load() -> None:
    while True:
        load = _load_1min()
        print(f"load_1min={load:.2f}")
        if load <= LOAD_CEILING:
            return
        print(f"load {load:.2f} is above {LOAD_CEILING}, waiting")
        time.sleep(15)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_ready(base: str) -> dict[str, object]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        try:
            last = _get_json(f"{base}/health")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        speaking = last["speaking"]
        hearing = last["hearing"]
        if not isinstance(speaking, dict) or not isinstance(hearing, dict):
            time.sleep(0.5)
            continue
        print(
            "health",
            f"speaking_ready={speaking['ready']}",
            f"hearing_ready={hearing['ready']}",
            f"card_memory_mb={last['card_memory_mb']}",
        )
        if speaking["ready"] is True and hearing["ready"] is True:
            return last
        time.sleep(0.5)
    message = f"models not ready within {READY_TIMEOUT_SECONDS}s: {last}"
    raise SystemExit(message)


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


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    total = sum(sample * sample for sample in samples)
    return (total / len(samples)) ** 0.5 / 32768.0


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


def _speak_measured(base: str, text: str) -> tuple[bytes, float, float]:
    request = urllib.request.Request(
        f"{base}/speak",
        data=json.dumps({"text": text, "language": "de"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    first_byte_at = None
    chunks = bytearray()
    with urllib.request.urlopen(request, timeout=60) as response:
        while True:
            piece = response.read(4096)
            if not piece:
                break
            if first_byte_at is None:
                first_byte_at = time.perf_counter()
            chunks.extend(piece)
    end = time.perf_counter()
    if first_byte_at is None:
        message = "speak returned no bytes"
        raise SystemExit(message)
    return bytes(chunks), first_byte_at - start, end - start


def _nvidia_memory() -> str:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        return f"unavailable: {error}"
    return completed.stdout.strip()


def main() -> None:
    """Run the stage proof under the probe-stack lock."""
    _wait_for_load()
    LOCK_PATH.touch()
    with LOCK_PATH.open("a", encoding="utf-8") as lock:
        print("waiting_for_lock", LOCK_PATH)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        print("lock_acquired", LOCK_PATH)
        print("gpu_before", _nvidia_memory())
        port = _free_port()
        env = os.environ.copy()
        env["SPEECH_HOST"] = "127.0.0.1"
        env["SPEECH_PORT"] = str(port)
        env["SPEECH_DEVICE"] = "cuda"
        env["SPEECH_SPEAKING_MODEL"] = "de_DE-thorsten-medium"
        env["SPEECH_HEARING_MODEL"] = "Systran/faster-whisper-large-v3"
        env["SPEECH_DEBUG"] = "false"
        cuda_dirs = ":".join(str(path) for path in cuda_library_dirs())
        if cuda_dirs:
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = (
                f"{cuda_dirs}:{existing}" if existing else cuda_dirs
            )
        project = Path(__file__).resolve().parents[1]
        server = subprocess.Popen(
            [sys.executable, "-m", "speech"],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            health = _wait_ready(base)
            print("health_ready", json.dumps(health, sort_keys=True))
            print("gpu_both_resident", _nvidia_memory())
            print("whisper_cache", WHISPER_CACHE)
            print("piper_cache", PIPER_CACHE)

            wav, ttfa, total = _speak_measured(base, SENTENCE)
            rate, pcm = _pcm_from_wav(wav)
            duration = (len(pcm) / 2) / rate
            print(
                "speak",
                f"ttfa_s={ttfa:.3f}",
                f"total_s={total:.3f}",
                f"duration_s={duration:.3f}",
                f"rms={_rms(pcm):.4f}",
                f"rate={rate}",
                f"bytes={len(wav)}",
            )
            if _rms(pcm) < 0.01:
                raise SystemExit("speak produced silence")
            if duration < 1.0:
                raise SystemExit("speak produced too little audio to be a sentence")

            client = project / "scripts" / "contract_client.py"
            print("contract_client_start")
            completed = subprocess.run(
                [sys.executable, str(client), base],
                check=False,
                cwd="/tmp",
                env=env,
                capture_output=True,
                text=True,
            )
            sys.stdout.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            if completed.returncode != 0:
                message = f"contract client failed: {completed.returncode}"
                raise SystemExit(message)
            print("gpu_after", _nvidia_memory())
            print("caches_kept")
            print("  huggingface:", HF_CACHE)
            print("  piper:", PIPER_CACHE)
            print("PROOF_OK")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            if server.stdout is not None:
                leftover = server.stdout.read()
                if leftover:
                    print("server_log_tail:")
                    print(leftover[-4000:])
            print("server_exit", server.returncode)
            print("gpu_released", _nvidia_memory())
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            print("lock_released")


if __name__ == "__main__":
    main()
