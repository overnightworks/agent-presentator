"""First start, the sliding session, and logout."""

from datetime import UTC, datetime, timedelta

import pytest

from presentator.application.identity import IDLE_WINDOW, Identity
from presentator.contracts.models import FirstStartClosedError, Role
from tests.application.fakes import (
    CountingIdentifierFactory,
    FakeLoginAttemptStore,
    FakeSessionRecordStore,
    FakeUserStore,
    FrozenClock,
    MatchingLiveness,
    ReversibleHasher,
)

_TYPED_WORDS = "the words only this test types"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(instant=datetime(2026, 1, 15, 9, tzinfo=UTC))


@pytest.fixture
def identity(clock: FrozenClock) -> Identity:
    return Identity(
        users=FakeUserStore(),
        sessions=FakeSessionRecordStore(clock=clock),
        attempts=FakeLoginAttemptStore(clock=clock),
        hasher=ReversibleHasher(),
        clock=clock,
        identifiers=CountingIdentifierFactory(),
        liveness=MatchingLiveness(),
    )


def sign_up_the_admin(identity: Identity, *, username: str = "felix") -> str:
    return identity.create_first_admin(username=username, password=_TYPED_WORDS)


def test_first_start_is_offered_while_the_instance_has_no_account(
    identity: Identity,
) -> None:
    assert identity.first_start_is_open() is True

    sign_up_the_admin(identity)

    assert identity.first_start_is_open() is False


def test_first_start_creates_an_admin_and_signs_them_in(identity: Identity) -> None:
    session_id = sign_up_the_admin(identity)

    signed_in = identity.signed_in_user(session_id)

    assert signed_in is not None
    assert signed_in.username == "felix"
    assert signed_in.role is Role.ADMIN


def test_first_start_refuses_a_second_account(identity: Identity) -> None:
    sign_up_the_admin(identity)

    with pytest.raises(FirstStartClosedError):
        identity.create_first_admin(username="stranger", password=_TYPED_WORDS)

    assert identity.account_named("stranger") is None


def test_a_login_survives_a_ninety_minute_talk(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    session_id = sign_up_the_admin(identity)

    for _ in range(6):
        clock.advance(timedelta(minutes=15))
        assert identity.signed_in_user(session_id) is not None


def test_a_session_expires_after_twelve_idle_hours(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    session_id = sign_up_the_admin(identity)

    clock.advance(IDLE_WINDOW + timedelta(seconds=1))

    assert identity.signed_in_user(session_id) is None


def test_the_idle_window_starts_again_at_every_request(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    session_id = sign_up_the_admin(identity)

    clock.advance(IDLE_WINDOW - timedelta(minutes=1))
    assert identity.signed_in_user(session_id) is not None
    clock.advance(IDLE_WINDOW - timedelta(minutes=1))

    assert identity.signed_in_user(session_id) is not None


def test_a_session_at_the_idle_boundary_is_still_alive(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    session_id = sign_up_the_admin(identity)

    clock.advance(IDLE_WINDOW)

    assert identity.signed_in_user(session_id) is not None


@pytest.mark.parametrize(
    "session_id",
    [
        pytest.param("", id="no-session-id"),
        pytest.param("a-session-nobody-opened", id="unknown-session"),
    ],
)
def test_only_a_known_live_session_names_a_person(
    identity: Identity,
    session_id: str,
) -> None:
    sign_up_the_admin(identity)

    assert identity.signed_in_user(session_id) is None


def test_logging_out_ends_the_session_the_id_stands_for(identity: Identity) -> None:
    session_id = sign_up_the_admin(identity)

    identity.log_out(session_id)

    assert identity.signed_in_user(session_id) is None


@pytest.mark.parametrize(
    "session_id",
    [
        pytest.param("", id="no-session-id"),
        pytest.param("a-session-nobody-opened", id="unknown-session"),
    ],
)
def test_logging_out_an_unknown_session_leaves_the_live_one_alone(
    identity: Identity,
    session_id: str,
) -> None:
    signed_in_session = sign_up_the_admin(identity)

    identity.log_out(session_id)

    assert identity.signed_in_user(signed_in_session) is not None
