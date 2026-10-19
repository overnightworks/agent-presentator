"""The sliding session and logout."""

from datetime import UTC, datetime, timedelta

import pytest

from presentator.application.identity import SESSION_LIFETIME, Identity
from presentator.contracts.models import Role, Session, User
from tests.application.fakes import (
    FakePeople,
    FakeSessionRecordStore,
    FakeUserStore,
    FrozenClock,
    MatchingLiveness,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(instant=datetime(2026, 1, 15, 9, tzinfo=UTC))


class LoggedOutDuringTheRequest(FakeSessionRecordStore):
    """A logout that commits between this request's load and its renewal."""

    def load(self, session_id: str) -> Session | None:
        loaded = super().load(session_id)
        self.delete(session_id)
        return loaded


@pytest.fixture
def people(clock: FrozenClock) -> FakePeople:
    return FakePeople(
        users=FakeUserStore(),
        sessions=FakeSessionRecordStore(clock=clock),
    )


@pytest.fixture
def identity(people: FakePeople) -> Identity:
    return identity_over(people)


def identity_over(people: FakePeople) -> Identity:
    return Identity(
        users=people.users,
        sessions=people.sessions,
        clock=people.clock,
        liveness=MatchingLiveness(),
    )


def sign_in_the_admin(people: FakePeople) -> str:
    return people.open_session_for(
        User(id="id-of-felix", username="felix", role=Role.ADMIN),
    )


def test_a_seeded_session_names_its_person(
    identity: Identity,
    people: FakePeople,
) -> None:
    session_id = sign_in_the_admin(people)

    signed_in = identity.signed_in_user(session_id)

    assert signed_in is not None
    assert signed_in.username == "felix"
    assert signed_in.role is Role.ADMIN


def test_a_login_survives_a_ninety_minute_talk(
    identity: Identity,
    people: FakePeople,
    clock: FrozenClock,
) -> None:
    session_id = sign_in_the_admin(people)

    for _ in range(6):
        clock.advance(timedelta(minutes=15))
        assert identity.signed_in_user(session_id) is not None


def test_an_unused_session_expires_after_the_window(
    identity: Identity,
    people: FakePeople,
    clock: FrozenClock,
) -> None:
    session_id = sign_in_the_admin(people)

    clock.advance(SESSION_LIFETIME + timedelta(seconds=1))

    assert identity.signed_in_user(session_id) is None


def test_a_session_at_the_idle_boundary_is_still_alive(
    identity: Identity,
    people: FakePeople,
    clock: FrozenClock,
) -> None:
    session_id = sign_in_the_admin(people)

    clock.advance(SESSION_LIFETIME)

    assert identity.signed_in_user(session_id) is not None


def test_a_logout_during_the_request_is_not_undone_by_its_renewal(
    clock: FrozenClock,
) -> None:
    sessions = LoggedOutDuringTheRequest(clock=clock)
    people = FakePeople(users=FakeUserStore(), sessions=sessions)
    identity = identity_over(people)
    session_id = sign_in_the_admin(people)

    assert identity.signed_in_user(session_id) is None
    assert sessions.rows == {}


@pytest.mark.parametrize(
    "session_id",
    [
        pytest.param("", id="no-session-id"),
        pytest.param("a-session-nobody-opened", id="unknown-session"),
    ],
)
def test_only_a_known_live_session_names_a_person(
    identity: Identity,
    people: FakePeople,
    session_id: str,
) -> None:
    sign_in_the_admin(people)

    assert identity.signed_in_user(session_id) is None


def test_logging_out_ends_the_session_the_id_stands_for(
    identity: Identity,
    people: FakePeople,
) -> None:
    session_id = sign_in_the_admin(people)

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
    people: FakePeople,
    session_id: str,
) -> None:
    signed_in_session = sign_in_the_admin(people)

    identity.log_out(session_id)

    assert identity.signed_in_user(signed_in_session) is not None
