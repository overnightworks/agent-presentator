"""Start the co-presenter against the operator's Claude login and local speech."""

from __future__ import annotations

import logging
import sys

import uvicorn

from copresenter.answer import ClaudeAnswerer
from copresenter.app import compose
from copresenter.config import load_settings

_log = logging.getLogger("copresenter")


def main() -> None:
    """Serve. Answering is the `claude` executable on PATH."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    settings = load_settings()
    answerer = ClaudeAnswerer(model=settings.claude_model)
    app = compose(settings, answerer=answerer)
    _log.info(
        "answering with %s via claude CLI, speech at %s, deck %s",
        settings.claude_model,
        settings.speech_url,
        settings.deck,
    )
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
