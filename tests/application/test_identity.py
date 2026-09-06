"""First start, login, the throttle, the sliding session, and logout."""

from datetime import UTC, datetime, timedelta

import pytest

from presentator.application.identity import (
    FAILURE_WINDOW,
    FAILURES_BEFORE_THROTTLE,
    IDLE_WINDOW,
    FirstStartClosedError,
    Identity,
)
from presentator.contracts.models import Role
from tests.application.fakes import (
    CountingIdentifierFactory,
    FakeLoginAttemptStore,
    FakeSessionRecordStore,
    FakeUserStore,
    FrozenClock,
    MarkingCookieSigner,
    ReversibleHasher,
)

_TYPED_WORDS = "the words only this test types"
_WRONG_WORDS = "guessed"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(instant=datetime(2026, 1, 15, 9, tzinfo=UTC))


@pytest.fixture
def identity(clock: FrozenClock) -> Identity:
    return Identity(
        users=FakeUserStore(),
        sessions=FakeSessionRecordStore(),
        attempts=FakeLoginAttemptStore(),
        hasher=ReversibleHasher(),
        clock=clock,
        identifiers=CountingIdentifierFactory(),
        cookies=MarkingCookieSigner(),
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
    cookie = sign_up_the_admin(identity)

    signed_in = identity.signed_in_user(cookie)

    assert signed_in is not None
    assert signed_in.username == "felix"
    assert signed_in.role is Role.ADMIN


def test_first_start_refuses_a_second_account(identity: Identity) -> None:
    sign_up_the_admin(identity)

    with pytest.raises(FirstStartClosedError, match="an admin creates"):
        identity.create_first_admin(username="stranger", password=_TYPED_WORDS)

    assert identity.log_in(username="stranger", password=_TYPED_WORDS) is None


def test_the_right_password_opens_a_session(identity: Identity) -> None:
    sign_up_the_admin(identity)

    cookie = identity.log_in(username="felix", password=_TYPED_WORDS)

    assert cookie is not None
    assert identity.signed_in_user(cookie) is not None


@pytest.mark.parametrize(
    ("username", "password"),
    [
        pytest.param("nobody", _TYPED_WORDS, id="unknown-name"),
        pytest.param("felix", _WRONG_WORDS, id="wrong-password"),
    ],
)
def test_a_refused_login_opens_no_session(
    identity: Identity,
    username: str,
    password: str,
) -> None:
    sign_up_the_admin(identity)

    assert identity.log_in(username=username, password=password) is None


def test_repeated_failures_throttle_even_the_right_password(
    identity: Identity,
) -> None:
    sign_up_the_admin(identity)
    for _ in range(FAILURES_BEFORE_THROTTLE):
        identity.log_in(username="felix", password=_WRONG_WORDS)

    assert identity.log_in(username="felix", password=_TYPED_WORDS) is None


def test_the_throttle_lets_go_once_the_failures_age_out(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    sign_up_the_admin(identity)
    for _ in range(FAILURES_BEFORE_THROTTLE):
        identity.log_in(username="felix", password=_WRONG_WORDS)

    clock.advance(FAILURE_WINDOW + timedelta(seconds=1))

    assert identity.log_in(username="felix", password=_TYPED_WORDS) is not None


def test_a_login_survives_a_ninety_minute_talk(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    cookie = sign_up_the_admin(identity)

    for _ in range(6):
        clock.advance(timedelta(minutes=15))
        assert identity.signed_in_user(cookie) is not None


def test_a_session_expires_after_twelve_idle_hours(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    cookie = sign_up_the_admin(identity)

    clock.advance(IDLE_WINDOW)

    assert identity.signed_in_user(cookie) is None


def test_the_idle_window_starts_again_at_every_request(
    identity: Identity,
    clock: FrozenClock,
) -> None:
    cookie = sign_up_the_admin(identity)

    clock.advance(IDLE_WINDOW - timedelta(minutes=1))
    assert identity.signed_in_user(cookie) is not None
    clock.advance(IDLE_WINDOW - timedelta(minutes=1))

    assert identity.signed_in_user(cookie) is not None


@pytest.mark.parametrize(
    "cookie",
    [
        pytest.param("", id="no-cookie"),
        pytest.param("forged-session", id="not-signed-here"),
        pytest.param("signed-unknown-session", id="signed-but-gone"),
    ],
)
def test_only_a_cookie_this_instance_signed_names_a_person(
    identity: Identity,
    cookie: str,
) -> None:
    sign_up_the_admin(identity)

    assert identity.signed_in_user(cookie) is None


def test_logging_out_ends_the_session_the_cookie_stands_for(
    identity: Identity,
) -> None:
    cookie = sign_up_the_admin(identity)

    identity.log_out(cookie)

    assert identity.signed_in_user(cookie) is None


@pytest.mark.parametrize(
    "cookie",
    [
        pytest.param("", id="no-cookie"),
        pytest.param("forged-session", id="not-signed-here"),
    ],
)
def test_logging_out_without_a_valid_cookie_leaves_the_session_alone(
    identity: Identity,
    cookie: str,
) -> None:
    signed_in_cookie = sign_up_the_admin(identity)

    identity.log_out(cookie)

    assert identity.signed_in_user(signed_in_cookie) is not None
