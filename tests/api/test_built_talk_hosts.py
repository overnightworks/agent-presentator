"""A talk and the policy around it name no host but this origin."""

import re
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest

from presentator.api.talk import talk_content_security_policy
from presentator.contracts.decks import talk_address
from tests.api.lobby import A_TALK_ADDRESS, a_content_security_policy
from tests.api.talk_support import A_DECK, two_hosts

# Outside `form-action`, a source names no host when it is 'self', 'none',
# 'unsafe-inline', a hash or nonce, or the data: or blob: scheme; every other
# spelling can admit one ('strict-dynamic' makes the browser ignore host sources).
_A_SOURCE_WITHOUT_A_HOST: Final = re.compile(
    r"'(?:self|none|unsafe-inline|(?:sha(?:256|384|512)|nonce)-[^']+)'|data:|blob:"
)
_ADDRESS_ATTRIBUTE: Final = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']*)["']""")
_AN_ADDRESS_ON_ANOTHER_HOST: Final = re.compile(
    r"(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE
)


def _sources_that_may_admit_a_host(policy: str) -> list[tuple[str, str]]:
    directives = [part.split() for part in policy.split(";") if part.strip()]
    return [
        (name, source)
        for name, *sources in directives
        if name != "form-action"
        for source in sources
        if not _A_SOURCE_WITHOUT_A_HOST.fullmatch(source)
    ]


@pytest.mark.parametrize(
    ("policy", "admitted"),
    [
        ("default-src 'self'; img-src *", ("img-src", "*")),
        ("default-src 'self'; connect-src 'self' wss:", ("connect-src", "wss:")),
        ("default-src 'self'; script-src https:", ("script-src", "https:")),
        ("default-src 'self'; font-src localhost:8000", ("font-src", "localhost:8000")),
        ("default-src 'self'; img-src CDN.Example.COM", ("img-src", "CDN.Example.COM")),
        (
            "default-src 'self'; script-src 'self' 'strict-dynamic'",
            ("script-src", "'strict-dynamic'"),
        ),
    ],
)
def test_a_policy_source_that_may_admit_a_host_is_found(
    policy: str,
    admitted: tuple[str, str],
) -> None:
    assert _sources_that_may_admit_a_host(policy) == [admitted]


def test_the_content_security_policy_admits_no_host_but_this_origin() -> None:
    assert _sources_that_may_admit_a_host(a_content_security_policy()) == []


# T13.
def test_the_talk_hosts_policy_admits_no_host_at_all() -> None:
    policy = talk_content_security_policy()

    assert _sources_that_may_admit_a_host(policy) == []
    assert "form-action 'self'" in policy


@pytest.mark.parametrize("page_name", ["", "404.html"])
def test_a_built_talk_page_points_only_at_its_own_origin(
    page_name: str, tmp_path: Path
) -> None:
    talk_host = two_hosts(tmp_path).signed_in_at(A_TALK_ADDRESS)

    page = talk_host.get(talk_address(A_DECK) + page_name)

    assert page.status_code == HTTPStatus.OK
    foreign_addresses = [
        address
        for address in _ADDRESS_ATTRIBUTE.findall(page.text)
        if _AN_ADDRESS_ON_ANOTHER_HOST.match(address)
    ]
    assert foreign_addresses == []
