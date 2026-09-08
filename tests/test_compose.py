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
