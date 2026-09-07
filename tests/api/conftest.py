"""The lobbies every route test starts from."""

import pytest

from tests.api.lobby import Lobby, a_lobby


@pytest.fixture
def lobby() -> Lobby:
    return a_lobby()


@pytest.fixture
def signed_in_lobby(lobby: Lobby) -> Lobby:
    lobby.set_up_admin()
    return lobby
