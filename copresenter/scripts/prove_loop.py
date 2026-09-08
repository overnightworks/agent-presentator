"""Drive the overlay in a real browser.

Default: the speech stand-in and a canned answerer. Prints `ran: stand-in`.
`--real` requires the speech service at `COPRESENTER_SPEECH_URL` and the
installed `claude` executable, synthesises a German question through `/speak`,
resamples it to the hearing rate, streams 100 ms PCM frames plus trailing
silence into `/hear`, drives a real Claude turn, then toggles off during
playback. Prints `ran: real` only after every assertion. On failure prints
`ran: real, proof: FAILED` with the failing assertion. Refuses to start if
the speech service is not ready or if `/health` names the stand-in's
speaking or hearing identity (`stand-in`), and never names a stand-in run
`real`.

Not a product test. Holds `/tmp/probe-stack.lock` while it binds ports.
"""

from __future__ import annotations

import argparse
import array
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import IO

import httpx
import uvicorn

from copresenter.answer import CannedAnswerer
from copresenter.app import compose
from copresenter.config import Settings
from copresenter.standin import create_standin
from copresenter.wav import SAMPLE_RATE

REPO = Path(__file__).resolve().parents[2]
COPRESENTER_DIR = Path(__file__).resolve().parents[1]
DECK = REPO / "examples" / "copresenter-deck"
FRONTEND = REPO / "frontend"
TALK = Path("/tmp/copresenter-talk")
REPORT = Path("/tmp/copresenter-proof.json")
LOCK = Path("/tmp/probe-stack.lock")
COPRESENTER_LOG = Path("/tmp/copresenter-real.log")

QUESTION = "Was steht auf der ersten Folie?"
HEAR_SAMPLE_RATE = SAMPLE_RATE
FRAME_MS = 100
TRAILING_SILENCE_S = 1.2
FRAME_GAP_MS = 15
WAV_HEADER_BYTES = 44
STREAMING_DATA_SIZE = 0xFFFFFFFF
_RIFF = b"RIFF"
_WAVE = b"WAVE"
_DATA = b"data"
_RATE_AT = 24
_RATE_BYTES = 4
_CHUNK_HEADER_BYTES = 8
_NESTED_SESSION_ENV_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "AI_AGENT")
CHROME_ARGS = (
    "--headless=new",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--mute-audio",
)
_REAL_TRANSCRIPT_TIMEOUT_MS = 20_000
_REAL_ANSWER_TIMEOUT_MS = 60_000
_SPEECH_HEALTH_TIMEOUT = 2.0
_WHO_TIMEOUT = 2.0
_SPEAK_TIMEOUT = 30.0
_TERMINATE_SECONDS = 10
_KILL_SECONDS = 5
_STANDIN_SPEAKING_MODEL = "stand-in"
_STANDIN_HEARING_MODEL = "stand-in"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(app, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.started:
            return server
        time.sleep(0.05)
    message = f"server on {host}:{port} did not start"
    raise RuntimeError(message)


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    message = f"{url} did not answer"
    raise RuntimeError(message)


def _build_talk() -> None:
    TALK.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "pnpm",
            "exec",
            "slidev",
            "build",
            str(DECK / "slides.md"),
            "--out",
            str(TALK),
            "--base",
            "/",
        ],
        check=True,
        cwd=FRONTEND,
    )


def _pcm_frame() -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE // 10)


def _pcm_from_wav(data: bytes) -> tuple[int, bytes]:
    """Sample rate and PCM of a 16-bit mono WAV, including a streaming size."""
    if len(data) < WAV_HEADER_BYTES or data[:4] != _RIFF or data[8:12] != _WAVE:
        message = "not a WAV body"
        raise ValueError(message)
    rate = int.from_bytes(data[_RATE_AT : _RATE_AT + _RATE_BYTES], "little")
    position = 12
    while position + _CHUNK_HEADER_BYTES <= len(data):
        chunk_id = data[position : position + 4]
        chunk_size = int.from_bytes(
            data[position + 4 : position + _CHUNK_HEADER_BYTES],
            "little",
        )
        payload_at = position + _CHUNK_HEADER_BYTES
        if chunk_id == _DATA:
            return rate, data[payload_at:]
        if chunk_size == STREAMING_DATA_SIZE:
            message = "WAV chunk before data has no length"
            raise ValueError(message)
        position = payload_at + chunk_size
    message = "WAV has no data chunk"
    raise ValueError(message)


def _resample_linear(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate:
        return pcm
    source = array.array("h")
    source.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not source:
        return b""
    ratio = from_rate / to_rate
    out_len = max(1, round(len(source) / ratio))
    out = array.array("h", [0]) * out_len
    last = len(source) - 1
    for index in range(out_len):
        pos = index * ratio
        low = int(pos)
        high = min(low + 1, last)
        frac = pos - low
        out[index] = int(source[low] * (1 - frac) + source[high] * frac)
    return out.tobytes()


def _chunk_frames(pcm: bytes, rate: int, frame_ms: int) -> list[bytes]:
    frame_bytes = int(rate * frame_ms / 1000) * 2
    return [pcm[index : index + frame_bytes] for index in range(0, len(pcm), frame_bytes)]


def _synthesize_question_frames(speech_url: str, text: str) -> list[bytes]:
    """Speak `text` on the real service and return 16 kHz PCM frames + silence."""
    response = httpx.post(
        f"{speech_url}/speak",
        json={"text": text, "language": "de"},
        timeout=_SPEAK_TIMEOUT,
    )
    response.raise_for_status()
    rate, pcm = _pcm_from_wav(response.content)
    resampled = _resample_linear(pcm, rate, HEAR_SAMPLE_RATE)
    silence = b"\x00\x00" * int(HEAR_SAMPLE_RATE * TRAILING_SILENCE_S)
    return _chunk_frames(resampled + silence, HEAR_SAMPLE_RATE, FRAME_MS)


def _send_frames(page, frames: list[bytes]) -> None:
    for frame in frames:
        page.evaluate(
            """(bytes) => {
                const arr = Uint8Array.from(bytes)
                return window.__copresenter.sendPcm(arr.buffer)
            }""",
            list(frame),
        )
        page.wait_for_timeout(FRAME_GAP_MS)


def _wait_ready(page) -> None:
    page.wait_for_function("() => window.__copresenter")


def _turn_on_local(page) -> None:
    page.evaluate("() => window.__copresenter.setOn(true)")
    page.wait_for_function(
        "() => window.__copresenter.snapshot().on === true"
        " && window.__copresenter.snapshot().hearing === 'local'"
        " && window.__copresenter.snapshot().hearOpen === true"
    )


def _drive_loop(page) -> dict[str, object]:
    _turn_on_local(page)
    sent = page.evaluate(
        """(bytes) => {
            const frame = Uint8Array.from(bytes)
            return window.__copresenter.sendPcm(frame.buffer)
        }""",
        list(_pcm_frame()),
    )
    page.wait_for_function(
        "() => window.__copresenter.snapshot().heard.includes('Folie')",
        timeout=15000,
    )
    page.wait_for_function(
        """() => {
            const snap = window.__copresenter.snapshot()
            return snap.answer.length > 0 && snap.audioSeconds > 0 && snap.speaking === false
        }""",
        timeout=20000,
    )
    snap = page.evaluate("() => window.__copresenter.snapshot()")
    page.evaluate("() => window.__copresenter.setOn(false)")
    page.wait_for_function("() => window.__copresenter.snapshot().on === false")
    off = page.evaluate("() => window.__copresenter.snapshot()")
    snap["sent_pcm"] = bool(sent)
    snap["off"] = off
    return snap


def _drive_off_during_activation(page) -> dict[str, object]:
    page.evaluate(
        """() => {
            window.__copresenter.setOn(true)
            window.__copresenter.setOn(false)
        }"""
    )
    page.wait_for_timeout(1000)
    snap = page.evaluate("() => window.__copresenter.snapshot()")
    return {
        "on": snap["on"],
        "hearOpen": snap["hearOpen"],
        "micLive": snap["micLive"],
        "hearing": snap["hearing"],
    }


def _drive_off_during_playback(page) -> dict[str, object]:
    _turn_on_local(page)
    page.evaluate("() => window.__copresenter.say('Was steht auf dieser Folie?')")
    page.wait_for_function(
        "() => window.__copresenter.snapshot().audioPlaying === true",
        timeout=20000,
    )
    page.evaluate("() => window.__copresenter.setOn(false)")
    page.wait_for_function("() => window.__copresenter.snapshot().on === false")
    snap = page.evaluate("() => window.__copresenter.snapshot()")
    return {
        "on": snap["on"],
        "speaking": snap["speaking"],
        "audioPlaying": snap["audioPlaying"],
        "hearOpen": snap["hearOpen"],
        "micLive": snap["micLive"],
        "workletLive": snap["workletLive"],
    }


def _drive_socket_close(page) -> dict[str, object]:
    _turn_on_local(page)
    page.evaluate("() => window.__copresenter.closeHear()")
    page.wait_for_function(
        """() => {
            const snap = window.__copresenter.snapshot()
            return snap.on === true && Boolean(snap.error)
        }""",
        timeout=10000,
    )
    snap = page.evaluate("() => window.__copresenter.snapshot()")
    page.evaluate("() => window.__copresenter.setOn(false)")
    return {
        "on_during_error": True,
        "error": snap["error"],
        "hearOpen": snap["hearOpen"],
        "hearing": snap["hearing"],
    }


def _drive_late_microphone_after_fallback(page) -> dict[str, object]:
    page.evaluate(
        """() => {
            const devices = navigator.mediaDevices
            const original = devices.getUserMedia.bind(devices)
            let release
            let pendingFirst = true
            const gate = new Promise((resolve) => { release = resolve })
            window.__releaseDelayedMic = () => release()
            window.__delayedMicStream = null
            devices.getUserMedia = async (constraints) => {
                if (!pendingFirst) return original(constraints)
                pendingFirst = false
                await gate
                const stream = await original(constraints)
                window.__delayedMicStream = stream
                return stream
            }
            window.__restoreGetUserMedia = () => {
                devices.getUserMedia = original
            }
        }"""
    )
    try:
        page.evaluate("() => window.__copresenter.setOn(true)")
        page.wait_for_function(
            "() => window.__copresenter.snapshot().on === true"
            " && window.__copresenter.snapshot().hearing === 'local'"
            " && window.__copresenter.snapshot().hearOpen === true"
        )
        page.evaluate("() => window.__copresenter.closeHear()")
        page.wait_for_function(
            """() => {
                const snap = window.__copresenter.snapshot()
                return snap.on === true && snap.hearing !== 'local' && Boolean(snap.error)
            }""",
            timeout=10000,
        )
        page.evaluate("() => window.__releaseDelayedMic()")
        page.wait_for_function(
            """() => {
                const stream = window.__delayedMicStream
                return Boolean(stream)
                    && stream.getTracks().length > 0
                    && stream.getTracks().every((track) => track.readyState === 'ended')
            }""",
            timeout=10000,
        )
        snap = page.evaluate(
            """() => {
                const stream = window.__delayedMicStream
                const tracks = stream ? stream.getTracks() : []
                const current = window.__copresenter.snapshot()
                return {
                    on: current.on,
                    hearing: current.hearing,
                    hearOpen: current.hearOpen,
                    micLive: current.micLive,
                    workletLive: current.workletLive,
                    error: current.error,
                    lateTracksEnded: tracks.length > 0
                        && tracks.every((track) => track.readyState === 'ended'),
                }
            }"""
        )
        page.evaluate("() => window.__copresenter.setOn(false)")
        return snap
    finally:
        page.evaluate(
            """() => {
                if (window.__releaseDelayedMic) window.__releaseDelayedMic()
                if (window.__restoreGetUserMedia) window.__restoreGetUserMedia()
            }"""
        )


def _drive_against_speech(page, talk_url: str) -> dict[str, object]:
    page.goto(talk_url, wait_until="networkidle")
    _wait_ready(page)
    loop = _drive_loop(page)
    off_activation = _drive_off_during_activation(page)
    off_playback = _drive_off_during_playback(page)
    closed = _drive_socket_close(page)
    late_microphone = _drive_late_microphone_after_fallback(page)
    return {
        "loop": loop,
        "off_during_activation": off_activation,
        "off_during_playback": off_playback,
        "socket_close": closed,
        "late_microphone": late_microphone,
    }


def _speech_ready(url: str) -> tuple[bool, dict[str, object]]:
    try:
        payload = httpx.get(f"{url}/health", timeout=_SPEECH_HEALTH_TIMEOUT).json()
    except (httpx.HTTPError, ValueError, TypeError):
        return False, {}
    if not isinstance(payload, dict):
        return False, {}
    speaking = payload.get("speaking")
    hearing = payload.get("hearing")
    if not isinstance(speaking, dict) or not isinstance(hearing, dict):
        return False, payload
    ready = speaking.get("ready") is True and hearing.get("ready") is True
    return ready, payload


def _health_identities_are_real(payload: dict[str, object]) -> bool:
    speaking = payload.get("speaking")
    hearing = payload.get("hearing")
    if not isinstance(speaking, dict) or not isinstance(hearing, dict):
        return False
    speaking_model = speaking.get("model")
    hearing_model = hearing.get("model")
    if not isinstance(speaking_model, str) or not isinstance(hearing_model, str):
        return False
    return speaking_model != _STANDIN_SPEAKING_MODEL and hearing_model != _STANDIN_HEARING_MODEL


def _chrome_path() -> str:
    found = shutil.which("google-chrome")
    if found:
        return found
    message = "google-chrome is required on PATH"
    raise RuntimeError(message)


def _open_browser() -> tuple[object, object, object]:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        executable_path=_chrome_path(),
        args=list(CHROME_ARGS),
    )
    context = browser.new_context(permissions=["microphone"])
    return playwright, browser, context


def _close_browser(playwright, browser, context) -> None:
    context.close()
    browser.close()
    playwright.stop()


class TalkHandler(SimpleHTTPRequestHandler):
    """Serve the static talk build for the browser proof."""

    def __init__(self, *args, **kwargs):
        """Serve files from the built talk directory."""
        super().__init__(*args, directory=str(TALK), **kwargs)

    def log_message(self, fmt: str, *args: object) -> None:
        """Stay quiet; the proof reports through stdout."""
        del fmt, args


def _ok_standin(result: dict[str, object]) -> bool:
    loop = result["loop"]
    off_act = result["off_during_activation"]
    off_play = result["off_during_playback"]
    closed = result["socket_close"]
    late = result["late_microphone"]
    loop_ok = bool(loop.get("heard") and loop.get("answer") and loop.get("audioSeconds"))
    off_ok = _off_released(loop.get("off"))
    activation_ok = not (off_act.get("on") or off_act.get("hearOpen") or off_act.get("micLive"))
    playback_ok = _off_released(off_play)
    closed_ok = bool(closed.get("error"))
    late_ok = (
        late.get("on") is True
        and late.get("hearing") == "browser"
        and not late.get("micLive")
        and not late.get("workletLive")
        and not late.get("hearOpen")
        and bool(late.get("lateTracksEnded"))
    )
    return loop_ok and off_ok and activation_ok and playback_ok and closed_ok and late_ok


def _off_released(off: object) -> bool:
    if not isinstance(off, dict):
        return False
    return (
        off.get("on") is False
        and off.get("speaking") is False
        and off.get("audioPlaying") is False
        and off.get("hearOpen") is False
        and off.get("micLive") is False
        and off.get("workletLive") is False
    )


def _real_failure(result: dict[str, object]) -> str | None:
    heard = result.get("heard")
    if not isinstance(heard, str) or not heard.strip():
        return "heard"
    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return "answer"
    audio = result.get("audio_seconds")
    if not isinstance(audio, (int, float)) or audio <= 0:
        return "audio_seconds"
    if not _off_released(result.get("off_during_playback")):
        return "off_during_playback"
    return None


def _emit(report: dict[str, object]) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    REPORT.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def _browser_standin(talk_url: str, present_url: str) -> tuple[dict[str, object], bool]:
    playwright, browser, context = _open_browser()
    try:
        page = context.new_page()
        result = _drive_against_speech(page, talk_url)
        who = httpx.get(f"{present_url}/who", timeout=_WHO_TIMEOUT).json()
    finally:
        _close_browser(playwright, browser, context)
    loop = result["loop"]
    report = {
        "ran": "stand-in",
        "hearing": loop.get("hearing"),
        "heard": loop.get("heard"),
        "answer": loop.get("answer"),
        "audio_seconds": loop.get("audioSeconds"),
        "sent_pcm": loop.get("sent_pcm"),
        "toggle_off": loop.get("off", {}).get("on") is False,
        "off_during_activation": result["off_during_activation"],
        "off_during_playback": result["off_during_playback"],
        "socket_close": result["socket_close"],
        "late_microphone": result["late_microphone"],
        "who": who,
    }
    _emit(report)
    return report, _ok_standin(result)


def _drive_real_turn(page, frames: list[bytes]) -> dict[str, object]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    _wait_ready(page)
    _turn_on_local(page)
    _send_frames(page, frames)
    try:
        page.wait_for_function(
            "() => window.__copresenter.snapshot().heard.length > 0",
            timeout=_REAL_TRANSCRIPT_TIMEOUT_MS,
        )
        page.wait_for_function(
            """() => {
                const snap = window.__copresenter.snapshot()
                return snap.answer.length > 0 && snap.audioSeconds > 0
                    && snap.speaking === false
            }""",
            timeout=_REAL_ANSWER_TIMEOUT_MS,
        )
    except PlaywrightTimeout as exc:
        snap = page.evaluate("() => window.__copresenter.snapshot()")
        return {
            "ran": "failed",
            "error_verbatim": str(exc),
            "last_snapshot": snap,
        }
    snap = page.evaluate("() => window.__copresenter.snapshot()")
    off = _drive_off_during_playback(page)
    return {
        "heard": snap.get("heard"),
        "answer": snap.get("answer"),
        "audio_seconds": snap.get("audioSeconds"),
        "error": snap.get("error"),
        "model": snap.get("model"),
        "off_during_playback": off,
    }


def _browser_real(talk_url: str, speech_url: str) -> dict[str, object]:
    frames = _synthesize_question_frames(speech_url, QUESTION)
    playwright, browser, context = _open_browser()
    try:
        page = context.new_page()
        page.goto(talk_url, wait_until="networkidle")
        return _drive_real_turn(page, frames)
    finally:
        _close_browser(playwright, browser, context)


def _scrub_nested_session_env(env: dict[str, str]) -> dict[str, str]:
    """Drop this runner's agent-session vars so the CLI child is a plain shell."""
    return {
        key: value
        for key, value in env.items()
        if not any(key.startswith(prefix) for prefix in _NESTED_SESSION_ENV_PREFIXES)
    }


def _start_copresenter(
    host: str,
    port: int,
    speech_url: str,
    talk_origin: str,
    log_file: IO[str],
) -> subprocess.Popen[bytes]:
    env = _scrub_nested_session_env(os.environ.copy())
    env["COPRESENTER_HOST"] = host
    env["COPRESENTER_PORT"] = str(port)
    env["COPRESENTER_SPEECH_URL"] = speech_url
    env["COPRESENTER_DECK"] = str(DECK)
    env["COPRESENTER_ALLOWED_ORIGIN"] = talk_origin
    return subprocess.Popen(
        ["uv", "run", "copresenter"],
        cwd=COPRESENTER_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait(timeout=_KILL_SECONDS)


def _claude_provider(who: object) -> bool:
    if not isinstance(who, dict):
        return False
    answerer = who.get("answerer")
    return isinstance(answerer, dict) and answerer.get("provider") == "claude"


def _run_standin() -> int:
    speech_server = None
    copresenter_server = None
    httpd = None
    try:
        speech_port = _free_port()
        present_port = _free_port()
        talk_port = _free_port()
        speech_url = f"http://127.0.0.1:{speech_port}"
        present_url = f"http://127.0.0.1:{present_port}"
        speech_server = _serve(create_standin(), "127.0.0.1", speech_port)
        settings = Settings(
            allowed_origin=f"http://127.0.0.1:{talk_port}",
            port=present_port,
            speech_url=speech_url,
            deck=DECK,
        )
        copresenter_server = _serve(
            compose(settings, answerer=CannedAnswerer()),
            "127.0.0.1",
            present_port,
        )
        _wait_http(f"{speech_url}/health")
        _wait_http(f"{present_url}/who")
        httpd = ThreadingHTTPServer(("127.0.0.1", talk_port), TalkHandler)
        Thread(target=httpd.serve_forever, daemon=True).start()
        talk_url = f"http://127.0.0.1:{talk_port}/?copresenter={present_url}"
        _report, ok = _browser_standin(talk_url, present_url)
        return 0 if ok else 1
    finally:
        if httpd is not None:
            httpd.shutdown()
        if copresenter_server is not None:
            copresenter_server.should_exit = True
        if speech_server is not None:
            speech_server.should_exit = True


def _real_preflight(speech_url: str) -> str | None:
    ready, health = _speech_ready(speech_url)
    if not ready:
        return f"real mode needs a ready speech service at {speech_url}: {health}"
    if not _health_identities_are_real(health):
        return (
            "real mode refuses a speech service whose speaking or hearing "
            f"model is the stand-in ({_STANDIN_SPEAKING_MODEL!r}/"
            f"{_STANDIN_HEARING_MODEL!r}) at {speech_url}: {health}"
        )
    if shutil.which("claude") is None:
        return "real mode needs the claude executable on PATH"
    return None


def _report_real(result: dict[str, object]) -> int:
    if result.get("ran") == "failed":
        _emit(result)
        return 1
    failed = _real_failure(result)
    if failed is not None:
        result["ran"] = "real, proof: FAILED"
        result["failed_assertion"] = failed
        _emit(result)
        return 1
    result["ran"] = "real"
    _emit(result)
    return 0


def _run_real() -> int:
    host = "127.0.0.1"
    present_port = _free_port()
    talk_port = _free_port()
    talk_origin = f"http://{host}:{talk_port}"
    speech_url = Settings(allowed_origin=talk_origin).speech_url.rstrip("/")
    blocked = _real_preflight(speech_url)
    if blocked is not None:
        sys.stderr.write(f"{blocked}\n")
        return 1
    present_url = f"http://{host}:{present_port}"
    httpd = None
    proc = None
    log_file = COPRESENTER_LOG.open("w", encoding="utf-8")
    try:
        proc = _start_copresenter(host, present_port, speech_url, talk_origin, log_file)
        _wait_http(f"{present_url}/who")
        who = httpx.get(f"{present_url}/who", timeout=_WHO_TIMEOUT).json()
        if not _claude_provider(who):
            report = {
                "ran": "failed",
                "who": who,
                "error_verbatim": "answerer is not the installed claude executable",
            }
            _emit(report)
            return 1
        httpd = ThreadingHTTPServer((host, talk_port), TalkHandler)
        Thread(target=httpd.serve_forever, daemon=True).start()
        talk_url = f"http://{host}:{talk_port}/?copresenter={present_url}"
        result = _browser_real(talk_url, speech_url)
        result["who"] = who
        return _report_real(result)
    finally:
        if httpd is not None:
            httpd.shutdown()
        _terminate(proc)
        log_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help=(
            "Require the speech service at COPRESENTER_SPEECH_URL and the "
            "installed claude executable; stream synthesised PCM into /hear"
        ),
    )
    args = parser.parse_args()
    load = Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]
    cpus = os.cpu_count() or 1
    if float(load) > 1.5 * cpus:
        sys.stderr.write(f"load {load} is too high to bind a probe stack\n")
        return 2
    lock = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        if not (FRONTEND / "node_modules").is_dir():
            subprocess.run(["pnpm", "install", "--frozen-lockfile"], check=True, cwd=FRONTEND)
        _build_talk()
        if args.real:
            return _run_real()
        return _run_standin()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
