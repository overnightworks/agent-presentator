"""Start the co-presenter against the operator's Claude login and local speech."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import uvicorn

from copresenter.answer import ClaudeAnswerer
from copresenter.app import compose
from copresenter.config import Settings, Transport, load_settings

_log = logging.getLogger("copresenter")
_DIRECTORY_MODE = 0o700
_SOCKET_MODE = 0o600


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
    if settings.transport is Transport.TCP:
        uvicorn.run(app, host=settings.host, port=settings.port)
        return
    with private_listener(settings) as listener:
        asyncio.run(_serve_unix(app, listener))


@contextmanager
def private_listener(settings: Settings) -> Iterator[socket.socket]:
    """Pre-bind the one same-UID private socket and remove only that inode."""
    if settings.runtime_uid != os.geteuid():
        message = "PRESENTATOR_RUNTIME_UID must equal the co-presenter process euid"
        raise RuntimeError(message)
    directory = settings.socket_path.parent
    _prepare_directory(directory)
    path = settings.socket_path
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        message = f"private socket already exists: {path}"
        raise RuntimeError(message)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    owned: os.stat_result | None = None
    try:
        listener.bind(str(path))
        path.chmod(_SOCKET_MODE)
        owned = path.lstat()
        if not stat.S_ISSOCK(owned.st_mode) or stat.S_IMODE(owned.st_mode) != _SOCKET_MODE:
            message = "private socket is not the required 0600 Unix socket"
            raise RuntimeError(message)
        yield listener
    finally:
        listener.close()
        if owned is not None:
            _unlink_owned(path, device=owned.st_dev, inode=owned.st_ino)


def _prepare_directory(directory: Path) -> None:
    with suppress(FileExistsError):
        directory.mkdir(mode=_DIRECTORY_MODE)
    status = directory.lstat()
    if not stat.S_ISDIR(status.st_mode):
        message = f"socket directory is not a directory: {directory}"
        raise RuntimeError(message)
    if status.st_uid != os.geteuid():
        message = f"socket directory belongs to another uid: {directory}"
        raise RuntimeError(message)
    if stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE:
        message = f"socket directory must have mode 0700: {directory}"
        raise RuntimeError(message)


def _unlink_owned(path: Path, *, device: int, inode: int) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if current.st_dev == device and current.st_ino == inode:
        path.unlink()


async def _serve_unix(app: object, listener: socket.socket) -> None:
    server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False))
    await server.serve(sockets=[listener])


if __name__ == "__main__":
    main()
