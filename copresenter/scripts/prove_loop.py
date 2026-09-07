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


def _drive(page, talk_url: str) -> dict[str, object]:
    page.goto(talk_url, wait_until="networkidle")
    page.wait_for_function("() => window.__copresenter")
    page.evaluate("() => window.__copresenter.setOn(true)")
    page.wait_for_function(
        "() => window.__copresenter.snapshot().on === true"
        " && window.__copresenter.snapshot().hearing === 'local'"
        " && window.__copresenter.snapshot().hearOpen === true"
    )
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

        class TalkHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(TALK), **kwargs)

            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

        httpd = ThreadingHTTPServer(("127.0.0.1", talk_port), TalkHandler)
        Thread(target=httpd.serve_forever, daemon=True).start()
        talk_url = f"http://127.0.0.1:{talk_port}/?copresenter={present_url}"
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
            context = browser.new_context(
                permissions=["microphone"],
            )
            page = context.new_page()
            snap = _drive(page, talk_url)
            who = httpx.get(f"{present_url}/who", timeout=2.0).json()
            browser.close()
        report = {
            "hearing": snap.get("hearing"),
            "heard": snap.get("heard"),
            "answer": snap.get("answer"),
            "audio_seconds": snap.get("audioSeconds"),
            "sent_pcm": snap.get("sent_pcm"),
            "toggle_off": snap.get("off", {}).get("on") is False,
            "who": who,
            "answerer": "canned",
            "speech": "stand-in",
        }
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        if not report["heard"] or not report["answer"] or not report["audio_seconds"]:
            return 1
        return 0
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
