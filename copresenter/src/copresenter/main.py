"""Start the co-presenter, or refuse without a provider key."""

from __future__ import annotations

import logging
import sys

import uvicorn

from copresenter.answer import ClaudeAnswerer
from copresenter.app import compose
from copresenter.config import MissingProviderKeyError, load_settings, provider_key

_log = logging.getLogger("copresenter")


def main() -> None:
    """Refuse without ANTHROPIC_API_KEY, then serve."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    settings = load_settings()
    try:
        key = provider_key()
    except MissingProviderKeyError as exc:
        _log.error("%s", exc)
        raise
    answerer = ClaudeAnswerer(api_key=key, model=settings.claude_model)
    app = compose(settings, answerer=answerer)
    _log.info(
        "answering with %s, speech at %s, deck %s",
        settings.claude_model,
        settings.speech_url,
        settings.deck,
    )
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
