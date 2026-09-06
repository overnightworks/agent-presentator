"""The invariants the identity types own themselves."""

from datetime import UTC, datetime, timedelta

import pytest

from presentator.contracts.models import Role, Session

_NOW = datetime(2026, 1, 15, 12, tzinfo=UTC)
_IDLE_WINDOW = timedelta(hours=12)


def test_an_instance_knows_only_the_admin_and_the_user_role() -> None:
    assert list(Role) == [Role.ADMIN, Role.USER]


@pytest.mark.parametrize(
    ("idle_for", "alive"),
    [
        pytest.param(timedelta(0), True, id="just-seen"),
        pytest.param(timedelta(minutes=90), True, id="through-a-talk"),
        pytest.param(_IDLE_WINDOW - timedelta(seconds=1), True, id="just-inside"),
        pytest.param(_IDLE_WINDOW, False, id="at-the-window"),
        pytest.param(_IDLE_WINDOW + timedelta(seconds=1), False, id="past-it"),
    ],
)
def test_a_session_lives_inside_the_idle_window_and_expires_at_its_boundary(
    idle_for: timedelta,
    *,
    alive: bool,
) -> None:
    session = Session(id="session-1", user_id="user-1", last_seen=_NOW)

    assert session.is_alive(at=_NOW + idle_for, idle_window=_IDLE_WINDOW) is alive
