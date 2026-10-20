"""What a Claude turn's child sees: one login variable, a closed environment, no leftovers."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_providers.config import reset_config
from agent_providers.errors import ProviderUnavailableError, SafeRouteReasonCode

from copresenter.answer import ClaudeAnswerer, provider_runtime
from copresenter.config import Settings
from copresenter.deck import load_deck

from .conftest import ALLOWED_ORIGIN, EXAMPLE_DECK

SYNTHETIC_LOGIN_LINE = "synthetic-setup-line"
FOREIGN_VARIABLE = "COPRESENTER_TEST_FOREIGN_SECRET"

# The fake reads its whole stdin as the real one does, announces no tool, and reports only
# presence flags it computes itself: it never prints the token it received.
FAKE_CLAUDE = """#!{python}
import json
import os
import sys
from pathlib import Path

sys.stdin.read()
environment = os.environ
token = environment.get("CLAUDE_CODE_OAUTH_TOKEN", "")
home = environment.get("HOME", "")
report = {{
    "token_present": bool(token),
    "token_elsewhere": any(
        value == token for name, value in environment.items() if name != "CLAUDE_CODE_OAUTH_TOKEN"
    ),
    "token_in_arguments": any(token in argument for argument in sys.argv[1:]),
    "api_key_present": "ANTHROPIC_API_KEY" in environment,
    "foreign_secret_present": "{foreign_secret}" in environment,
    "home": home,
    "credentials_file_present": (Path(home) / ".claude" / ".credentials.json").exists(),
}}
print(json.dumps({{"type": "system", "subtype": "init", "tools": [], "slash_commands": [],
                  "mcp_servers": [], "apiKeySource": "none"}}), flush=True)
if {fails}:
    sys.exit(1)
text = json.dumps(report)
message = {{"content": [{{"type": "text", "text": text}}]}}
print(json.dumps({{"type": "assistant", "message": message}}))
print(json.dumps({{"type": "result", "result": text}}))
"""


@dataclass(frozen=True)
class Claude:
    """A configured answerer and the directories the test looks at afterwards."""

    answerer: ClaudeAnswerer
    working_root: Path
    token_file: Path
    home: Path

    def reported(self) -> dict[str, object]:
        """Consume the whole turn and parse the fake's report out of the streamed text."""
        return json.loads(asyncio.run(self._whole_answer()))

    async def _whole_answer(self) -> str:
        deck = load_deck(EXAMPLE_DECK, language="de")
        deltas = [delta async for delta in self.answerer.stream(said="Hallo", slide=1, deck=deck)]
        return "".join(deltas)


def install_fake_claude(directory: Path, *, fails: bool) -> None:
    directory.mkdir()
    fake = directory / "claude"
    fake.write_text(
        FAKE_CLAUDE.format(
            python=sys.executable,
            foreign_secret=FOREIGN_VARIABLE,
            fails=fails,
        ),
        encoding="utf-8",
    )
    fake.chmod(0o700)


def write_token(path: Path, content: str = SYNTHETIC_LOGIN_LINE, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{content}\n", encoding="utf-8")
    path.chmod(mode)


def build_claude(
    tmp_path: Path,
    *,
    fails: bool = False,
    login: tuple[str, int] | None = (SYNTHETIC_LOGIN_LINE, 0o600),
) -> Claude:
    """Configure a turn whose shared token file holds `login` (content, mode), or is missing."""
    home = tmp_path / "home"
    home.mkdir()
    working_root = tmp_path / "root"
    working_root.mkdir(mode=0o700)
    install_fake_claude(tmp_path / "bin", fails=fails)
    token_file = home / ".secrets/agent-service/claude.token"
    if login is not None:
        write_token(token_file, *login)
    # Valid tokens beside the shared file: no per-app fallback may pick one up.
    for decoy in (
        token_file.with_name("copresenter.token"),
        home / ".config/copresenter/claude.token",
        tmp_path / "decoy/claude.token",
    ):
        write_token(decoy)
    settings = Settings(
        allowed_origin=ALLOWED_ORIGIN,
        claude_token_file=token_file,
        cli_search_path=(tmp_path / "bin",),
        cli_working_root=working_root,
    )
    runtime = provider_runtime(
        token_file=settings.claude_token_file,
        search_path=settings.cli_search_path,
        working_root=settings.cli_working_root,
    )
    return Claude(
        answerer=ClaudeAnswerer(model="claude-sonnet-4-6", runtime=runtime),
        working_root=working_root,
        token_file=token_file,
        home=home,
    )


@pytest.fixture(autouse=True)
def fresh_provider_runtime() -> Iterator[None]:
    reset_config()
    yield
    reset_config()


@pytest.fixture
def claude(tmp_path: Path) -> Claude:
    return build_claude(tmp_path)


def test_child_gets_the_shared_login_only_as_the_oauth_variable(
    claude: Claude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-api-key")
    monkeypatch.setenv(FOREIGN_VARIABLE, "synthetic-foreign-secret")

    report = claude.reported()

    assert report["token_present"] is True
    assert report["token_elsewhere"] is False
    assert report["token_in_arguments"] is False
    assert report["api_key_present"] is False
    assert report["foreign_secret_present"] is False


def test_child_home_lies_below_the_working_root_and_holds_no_credentials_file(
    claude: Claude,
) -> None:
    report = claude.reported()

    assert Path(str(report["home"])).is_relative_to(claude.working_root)
    assert report["credentials_file_present"] is False


def test_a_normal_turn_leaves_no_home_in_the_working_root(claude: Claude) -> None:
    claude.reported()

    assert list(claude.working_root.iterdir()) == []


def test_a_turn_that_ends_in_a_provider_error_leaves_no_home_in_the_working_root(
    tmp_path: Path,
) -> None:
    failing = build_claude(tmp_path, fails=True)

    with pytest.raises(ProviderUnavailableError):
        failing.reported()

    assert list(failing.working_root.iterdir()) == []


def refused_login(refusal: str) -> tuple[str, int] | None:
    """The token file content and mode for `refusal`; the content stays out of test arguments."""
    if refusal == "missing":
        return None
    if refusal == "json":
        return ('{"synthetic-json-body": "distinct-json-line"}', 0o600)
    if refusal == "api-key":
        return ("sk-ant-api03-distinct-synthetic-line", 0o600)
    return (SYNTHETIC_LOGIN_LINE, 0o644)


def leaks_refused_login(refusal: str, surfaces: tuple[str, ...]) -> bool:
    login = refused_login(refusal)
    return login is not None and any(login[0] in surface for surface in surfaces)


@pytest.mark.parametrize("refusal", ["missing", "json", "api-key", "readable-by-others"])
def test_a_missing_or_refused_shared_token_ends_the_turn_with_no_login_configured(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    refusal: str,
) -> None:
    claude = build_claude(tmp_path, login=refused_login(refusal))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ProviderUnavailableError) as ended:
        claude.reported()

    assert ended.value.reason is not None
    assert ended.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED
    assert list(claude.working_root.iterdir()) == []
    if refusal == "missing":
        assert not claude.token_file.exists()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert str(claude.token_file) in logged
    surfaces = (logged, str(ended.value), repr(ended.value))
    assert not leaks_refused_login(refusal, surfaces)
