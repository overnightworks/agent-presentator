"""What the compose file promises the server about this machine's Docker.

The server is handed names it later gives the daemon, and a name that drifts
from what Docker really made is a build reading somebody else's talks or none
at all. Nothing here starts a container; it reads the one file that must agree
with itself.
"""

from pathlib import Path

_COMPOSE = Path(__file__).parents[1] / "compose.yaml"
_THE_BUILDS_VOLUME = "builds"


def test_the_server_is_handed_the_very_volume_this_file_declares() -> None:
    written = _COMPOSE.read_text(encoding="utf-8")

    # Docker names a declared volume after the project it belongs to, so the
    # name the server is given and the volume that exists agree exactly while
    # this file derives the one and pins neither.
    assert (
        f"PRESENTATOR_BUILD_VOLUME: ${{COMPOSE_PROJECT_NAME}}_{_THE_BUILDS_VOLUME}"
        in written
    )
    lines = written.splitlines()
    assert f"  {_THE_BUILDS_VOLUME}:" in lines
    assert not [line for line in lines if line.strip().startswith("name:")]


def test_copresenter_uses_one_private_socket_and_runtime_uid() -> None:
    written = _COMPOSE.read_text(encoding="utf-8")

    services_using_uid = ("presentator", "build-sandbox")
    configured_uid = "PRESENTATOR_RUNTIME_UID: ${PRESENTATOR_RUNTIME_UID:"
    assert written.count(configured_uid) == len(services_using_uid)
    assert (
        "PRESENTATOR_COPRESENTER_SOCKET: /run/presentator-copresenter/copresenter.sock"
        in written
    )
    assert "PRESENTATOR_SPEECH_SOCKET: /run/presentator-speech/speech.sock" in written
    assert ":/run/presentator-copresenter" in written
    assert ":/run/presentator-speech" in written
    assert "extra_hosts:" not in written
    assert "3040:3040" not in written
    assert "8090:8090" not in written
    assert written.count("ports:") == 1
    assert written.count('"127.0.0.1:8000:8000"') == 1
