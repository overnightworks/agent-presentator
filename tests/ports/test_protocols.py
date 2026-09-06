"""The M0 seam ports are protocols, so a later slice can write stores against them."""

from typing import Protocol

import pytest

from presentator.ports.catalog import BuildRunner, DeckStore, SourceStore
from presentator.ports.identity import (
    Clock,
    LoginAttemptStore,
    PasswordHasher,
    SessionRecordStore,
    UserStore,
)


@pytest.mark.parametrize(
    "port",
    [
        UserStore,
        SessionRecordStore,
        LoginAttemptStore,
        PasswordHasher,
        Clock,
        DeckStore,
        SourceStore,
        BuildRunner,
    ],
)
def test_the_seam_ports_are_protocols(port: type) -> None:
    assert issubclass(port, Protocol)
