"""Pure construction and the functions that live on the M0 contracts."""

from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta

import pytest

from presentator.contracts.models import (
    BuildState,
    Deck,
    InvalidDeckSlugError,
    Role,
    Session,
    Source,
    User,
)

_NOW = datetime(2026, 1, 15, 12, tzinfo=UTC)
_IDLE_WINDOW = timedelta(seconds=10)


def a_user(
    *,
    user_id: str = "user-1",
    username: str = "operator",
    role: Role = Role.ADMIN,
) -> User:
    return User(id=user_id, username=username, role=role)


def a_session(
    *,
    session_id: str = "session-1",
    user_id: str = "user-1",
    last_seen: datetime = _NOW,
) -> Session:
    return Session(id=session_id, user_id=user_id, last_seen=last_seen)


def a_deck(
    *,
    slug: str = "knowledge-fabric",
    title: str = "Knowledge Fabric",
) -> Deck:
    return Deck(
        slug=slug,
        title=title,
        owner_id="user-1",
        source_id="source-1",
        changed_at=_NOW,
    )


def a_source(
    *,
    source_id: str = "source-1",
    owner_id: str = "user-1",
    url: str = "git@example.com:talks.git",
    ref: str = "main",
    credential_ref: str | None = None,
) -> Source:
    return Source(
        id=source_id,
        owner_id=owner_id,
        url=url,
        ref=ref,
        credential_ref=credential_ref,
    )


def test_role_is_closed_as_admin_and_user() -> None:
    assert list(Role) == [Role.ADMIN, Role.USER]


def test_build_state_is_closed() -> None:
    assert list(BuildState) == [
        BuildState.PENDING,
        BuildState.BUILDING,
        BuildState.SUCCEEDED,
        BuildState.FAILED,
    ]


@pytest.mark.parametrize(
    "directory_name",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot-dot"),
        pytest.param("talks/intro", id="slash"),
        pytest.param("talk\0intro", id="nul"),
    ],
)
def test_slug_from_rejects_a_name_that_is_not_a_folder_name(
    directory_name: str,
) -> None:
    with pytest.raises(InvalidDeckSlugError, match="folder name"):
        Deck.slug_from(directory_name)


def test_slug_from_is_the_folder_name() -> None:
    assert Deck.slug_from("knowledge-fabric") == "knowledge-fabric"


def test_title_does_not_change_the_slug() -> None:
    deck = a_deck(
        slug=Deck.slug_from("knowledge-fabric"),
        title="Knowledge Fabric v2",
    )
    assert deck.slug == "knowledge-fabric"


@pytest.mark.parametrize(
    ("elapsed", "alive"),
    [
        pytest.param(timedelta(0), True, id="just-seen"),
        pytest.param(_IDLE_WINDOW - timedelta(microseconds=1), True, id="just-inside"),
        pytest.param(_IDLE_WINDOW, False, id="at-the-window"),
        pytest.param(
            _IDLE_WINDOW + timedelta(microseconds=1),
            False,
            id="just-outside",
        ),
    ],
)
def test_session_is_alive_inside_the_idle_window_and_expired_at_its_boundary(
    elapsed: timedelta,
    *,
    alive: bool,
) -> None:
    session = a_session(last_seen=_NOW)
    assert session.id == "session-1"
    assert session.is_alive(at=_NOW + elapsed, idle_window=_IDLE_WINDOW) is alive


@pytest.mark.parametrize("model", [Deck, Source])
def test_owner_id_is_required_on_deck_and_source(
    model: type[Deck] | type[Source],
) -> None:
    owner = next(field for field in fields(model) if field.name == "owner_id")
    assert owner.default is MISSING
    assert owner.default_factory is MISSING


def test_a_user_keeps_the_role_it_was_built_with() -> None:
    user = a_user(role=Role.USER)
    assert user.id == "user-1"
    assert user.role is Role.USER
    assert user.username == "operator"


def test_a_deck_keeps_an_optional_build_pointer_and_last_error() -> None:
    deck = Deck(
        slug="knowledge-fabric",
        title="Knowledge Fabric",
        owner_id="user-1",
        source_id="source-1",
        changed_at=_NOW,
        active_build_path="/builds/talk",
        last_error="slidev failed",
    )
    assert deck.owner_id == "user-1"
    assert deck.changed_at == _NOW
    assert deck.active_build_path == "/builds/talk"
    assert deck.last_error == "slidev failed"


def test_a_source_keeps_an_optional_credential_reference() -> None:
    source = a_source(credential_ref="credential-1")
    assert source.id == "source-1"
    assert source.owner_id == "user-1"
    assert source.credential_ref == "credential-1"
