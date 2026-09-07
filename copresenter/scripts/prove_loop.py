"""Drive the overlay against the speech stand-in in a real browser.

Not a product test. Holds `/tmp/probe-stack.lock` while it binds ports.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import uvicorn

from copresenter.answer import CannedAnswerer
from copresenter.app import compose
from copresenter.config import Settings
from copresenter.standin import create_standin
from copresenter.wav import SAMPLE_RATE

REPO = Path(__file__).resolve().parents[2]
DECK = REPO / "examples" / "copresenter-deck"
FRONTEND = REPO / "frontend"
TALK = Path("/tmp/copresenter-talk")
REPORT = Path("/tmp/copresenter-proof.json")
LOCK = Path("/tmp/probe-stack.lock")
SPEECH_SERVICE = "http://127.0.0.1:8090"


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
        """() => {
            const snap = window.__copresenter.snapshot()
            return snap.speaking === true || snap.audioPlaying === true
        }""",
        timeout=15000,
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


def _speech_ready(url: str) -> bool:
    try:
        body = httpx.get(f"{url}/health", timeout=1.0).json()
    except (httpx.HTTPError, ValueError, TypeError):
        return False
    speaking = body.get("speaking") if isinstance(body, dict) else None
    if not isinstance(speaking, dict):
        return False
    return speaking.get("ready") is True


def _drive_real_speech(browser) -> dict[str, object]:
    talk_port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", talk_port), TalkHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    copresenter_server = None
    try:
        present_port = _free_port()
        settings = Settings(port=present_port, speech_url=SPEECH_SERVICE, deck=DECK)
        copresenter_server = _serve(
            compose(settings, answerer=CannedAnswerer()),
            "127.0.0.1",
            present_port,
        )
        _wait_http(f"http://127.0.0.1:{present_port}/who")
        talk_url = f"http://127.0.0.1:{talk_port}/?copresenter=http://127.0.0.1:{present_port}"
        page = browser.new_page()
        page.goto(talk_url, wait_until="networkidle")
        _wait_ready(page)
        page.evaluate("() => window.__copresenter.setOn(true)")
        page.wait_for_function("() => window.__copresenter.snapshot().on === true")
        page.evaluate("() => window.__copresenter.say('Was steht auf dieser Folie?')")
        page.wait_for_function(
            """() => {
                const snap = window.__copresenter.snapshot()
                return snap.answer.length > 0 && snap.audioSeconds > 0
            }""",
            timeout=30000,
        )
        snap = page.evaluate("() => window.__copresenter.snapshot()")
        page.evaluate("() => window.__copresenter.setOn(false)")
        who = httpx.get(f"http://127.0.0.1:{present_port}/who", timeout=2.0).json()
        return {
            "heard": snap.get("heard"),
            "answer": snap.get("answer"),
            "audio_seconds": snap.get("audioSeconds"),
            "who": who,
        }
    finally:
        httpd.shutdown()
        if copresenter_server is not None:
            copresenter_server.should_exit = True


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
    off_ok = loop.get("off", {}).get("on") is False
    activation_ok = not (off_act.get("on") or off_act.get("hearOpen") or off_act.get("micLive"))
    playback_ok = not (
        off_play.get("on") or off_play.get("audioPlaying") or off_play.get("speaking")
    )
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


def _browser_proof(talk_url: str, present_url: str) -> tuple[dict[str, object], bool]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=[
                "--headless=new",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--mute-audio",
            ],
        )
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        result = _drive_against_speech(page, talk_url)
        who = httpx.get(f"{present_url}/who", timeout=2.0).json()
        if _speech_ready(SPEECH_SERVICE):
            speech_8090: dict[str, object] | str = _drive_real_speech(context)
        else:
            speech_8090 = "skipped: GET /health on :8090 is not ready"
        browser.close()
    loop = result["loop"]
    report = {
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
        "speech_8090": speech_8090,
        "who": who,
        "answerer": "canned",
        "speech": "stand-in",
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report, _ok_standin(result)


def main() -> int:
    load = Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]
    if float(load) > 1.5 * os.cpu_count():
        sys.stderr.write(f"load {load} is too high to bind a probe stack\n")
        return 2
    lock = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_EX)
    speech_server = None
    copresenter_server = None
    httpd = None
    try:
        if not (FRONTEND / "node_modules").is_dir():
            subprocess.run(["pnpm", "install", "--frozen-lockfile"], check=True, cwd=FRONTEND)
        _build_talk()
        speech_port = _free_port()
        present_port = _free_port()
        talk_port = _free_port()
        speech_url = f"http://127.0.0.1:{speech_port}"
        present_url = f"http://127.0.0.1:{present_port}"
        speech_server = _serve(create_standin(), "127.0.0.1", speech_port)
        settings = Settings(port=present_port, speech_url=speech_url, deck=DECK)
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
        _report, ok = _browser_proof(talk_url, present_url)
        return 0 if ok else 1
    finally:
        if httpd is not None:
            httpd.shutdown()
        if copresenter_server is not None:
            copresenter_server.should_exit = True
        if speech_server is not None:
            speech_server.should_exit = True
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
