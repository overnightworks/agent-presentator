"""A talk and the policy around it name no host but this origin."""

import re
from pathlib import Path
from typing import Final

import pytest

from presentator.api.auth import CONTENT_SECURITY_POLICY

# What `slidev build` of `examples/hello-deck` writes, `--base /deck/hello-deck/`.
_BUILT_TALK: Final = Path(__file__).parent / "built_talk"
_A_FOREIGN_HOST: Final = re.compile(r"(?:https?:)?//|[a-z0-9-]+(?:\.[a-z0-9-]+)+\b")
_ADDRESS_ATTRIBUTE: Final = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']*)["']""")


def _addresses_in(page: Path) -> list[str]:
    return _ADDRESS_ATTRIBUTE.findall(page.read_text(encoding="utf-8"))


def test_the_content_security_policy_admits_no_host_but_this_origin() -> None:
    directives = [part.split() for part in CONTENT_SECURITY_POLICY.split(";")]

    foreign_sources = [
        (name, source)
        for name, *sources in directives
        if name != "form-action"
        for source in sources
        if _A_FOREIGN_HOST.search(source)
    ]

    assert foreign_sources == []


@pytest.mark.parametrize("page_name", ["index.html", "404.html"])
def test_a_built_talk_page_points_only_at_its_own_origin(page_name: str) -> None:
    foreign_addresses = [
        address
        for address in _addresses_in(_BUILT_TALK / page_name)
        if re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", address, re.IGNORECASE)
    ]

    assert foreign_addresses == []
