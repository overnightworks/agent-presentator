"""What the compose file promises the server about this machine's Docker.

The server is handed names it later gives the daemon, and a name that drifts
from what Docker really made is a build reading somebody else's talks or none
at all. Nothing here starts a container; it reads the one file that must agree
with itself.
"""

from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
import yaml

_COMPOSE = Path(__file__).parents[1] / "compose.yaml"
_DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"
_THE_BUILDS_VOLUME = "builds"
_OVERNIGHTWORKS_READ_ACCESS_ID = "overnightworks_read_token"
_OVERNIGHTWORKS_READ_ENV_VAR = "OVERNIGHTWORKS_READ_TOKEN"
_OVERNIGHTWORKS_READ_TOKEN_MOUNT = (
    f"--mount=type=secret,id={_OVERNIGHTWORKS_READ_ACCESS_ID},required=true"
)
_OVERNIGHTWORKS_READ_SENSITIVE_VALUES = (
    _OVERNIGHTWORKS_READ_ACCESS_ID,
    _OVERNIGHTWORKS_READ_ENV_VAR,
)


class _ServiceBuild(TypedDict, total=False):
    secrets: list[str]
    args: dict[str, str]


class _Service(TypedDict):
    build: _ServiceBuild
    # Both are runtime exposures the build-time secret mount must stay the
    # only path for: a plain environment entry or a service-level (not
    # build-level) secret would hand the running container the value.
    environment: NotRequired[dict[str, str] | list[str]]
    secrets: NotRequired[list[str]]


class _SecretSource(TypedDict):
    environment: str


class _ComposeFile(TypedDict):
    services: dict[str, _Service]
    secrets: dict[str, _SecretSource]


def _load_compose() -> _ComposeFile:
    # The shape this test pins is exactly what it asserts below; a file that
    # drifts from it fails those assertions, not this narrowing.
    return cast("_ComposeFile", yaml.safe_load(_COMPOSE.read_text(encoding="utf-8")))


def _dockerfile_run_instructions() -> list[str]:
    """Join each `RUN` and its backslash continuations into one instruction."""
    instructions: list[str] = []
    current: list[str] = []
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if current:
            current.append(line)
        elif line.startswith("RUN "):
            current = [line]
        else:
            continue
        if not line.rstrip().endswith("\\"):
            instructions.append("\n".join(current))
            current = []
    return instructions


_UV_SYNC_INSTRUCTIONS = [
    instruction
    for instruction in _dockerfile_run_instructions()
    if "uv sync" in instruction
]


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


def test_the_instance_build_alone_reads_the_overnightworks_read_token() -> None:
    services = _load_compose()["services"]

    # Only the instance's own build resolves webauth from source; the
    # toolchain layer build-sandbox reuses never needs the private repository.
    assert services["presentator"]["build"].get("secrets") == [
        _OVERNIGHTWORKS_READ_ACCESS_ID
    ]
    assert "secrets" not in services["build-sandbox"]["build"]
    assert (
        _load_compose()["secrets"][_OVERNIGHTWORKS_READ_ACCESS_ID]["environment"]
        == _OVERNIGHTWORKS_READ_ENV_VAR
    )


def _mentions_a_sensitive_value(value: object) -> bool:
    """Whether `value` carries the token or its secret id anywhere inside it.

    Walks dict values/keys and list entries so a mutation that moves the
    token into `environment`'s list form, a nested mapping, or alongside
    other entries is still caught.
    """
    if isinstance(value, str):
        return any(
            sensitive in value for sensitive in _OVERNIGHTWORKS_READ_SENSITIVE_VALUES
        )
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return any(
            _mentions_a_sensitive_value(key) or _mentions_a_sensitive_value(entry)
            for key, entry in mapping.items()
        )
    if isinstance(value, list):
        entries = cast("list[object]", value)
        return any(_mentions_a_sensitive_value(entry) for entry in entries)
    return False


@pytest.mark.parametrize("service_name", sorted(_load_compose()["services"]))
def test_no_service_exposes_the_overnightworks_read_token_at_runtime(
    service_name: str,
) -> None:
    service = _load_compose()["services"][service_name]

    # The BuildKit secret mount (asserted above) is the only sanctioned path
    # for this token; a runtime environment entry, a service-level secret
    # (distinct from the build-level mount), or a build argument would each
    # hand the daemon or the running container the value or its id instead.
    assert not _mentions_a_sensitive_value(service.get("environment"))
    assert not _mentions_a_sensitive_value(service.get("secrets"))
    assert not _mentions_a_sensitive_value(service["build"].get("args"))


def test_the_dockerfile_resolves_dependencies_through_uv_sync_at_least_once() -> None:
    # A parametrized test over zero cases passes vacuously; this pins that the
    # family below actually holds the instructions it claims to check.
    assert _UV_SYNC_INSTRUCTIONS


@pytest.mark.parametrize(
    "instruction", _UV_SYNC_INSTRUCTIONS, ids=range(len(_UV_SYNC_INSTRUCTIONS))
)
def test_every_uv_sync_mounts_the_required_overnightworks_read_token(
    instruction: str,
) -> None:
    # A dependency resolution that reaches webauth without the token must fail
    # loud rather than silently fetch an outdated or public fallback.
    assert _OVERNIGHTWORKS_READ_TOKEN_MOUNT in instruction
