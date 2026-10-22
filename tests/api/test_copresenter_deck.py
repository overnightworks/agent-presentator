"""E11 (#355): the co-presenter answers only about a built deck its asker owns."""

from __future__ import annotations

from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from webauth.cookies import DEFAULT_SESSION_COOKIE_NAME

from presentator.application.copresenter import AskLimiter, AskLimits
from presentator.contracts.models import Role
from tests.api.lobby import ADMIN
from tests.api.test_copresenter import (
    ASKED,
    NOW,
    QUESTION,
    a_copresenter_lobby,
    ask,
    the_admins_decks,
)
from tests.application.fakes import FakeUserStore, FrozenClock

if TYPE_CHECKING:
    from httpx2 import Response

    from presentator.contracts.decks import Deck
    from tests.api.lobby import Lobby
    from tests.api.test_copresenter import RecordingPrivateCoPresenter

TARGET = "target"
ONE_QUESTION_ONLY = AskLimits(per_window=1)
BUILT_TARGET = replace(ASKED.deck, slug=TARGET)
UNBUILT = replace(BUILT_TARGET, build=None)
ANOTHERS = replace(BUILT_TARGET, owner_id="id-of-someone-else")
SOURCELESS = replace(BUILT_TARGET, source_id="a-source-long-gone")


def ask_about(lobby: Lobby, slug: str) -> Response:
    return ask(lobby.client, {**QUESTION, "deck": slug})


def an_instance(
    *others: Deck, removed: tuple[str, ...] = ()
) -> tuple[Lobby, RecordingPrivateCoPresenter]:
    """An admin signed in, a budget of one question, and the admin's own deck."""
    decks = the_admins_decks()
    for deck in others:
        decks.put(deck)
        if deck.build is not None:
            decks.put_build(deck.slug, deck.build)
    decks.removed.update(removed)
    lobby, private, _clock = a_copresenter_lobby(
        decks=decks,
        limiter=AskLimiter(clock=FrozenClock(instant=NOW), limits=ONE_QUESTION_ONLY),
    )
    lobby.sign_in(role=Role.ADMIN)
    return lobby, private


def the_answer_to_a_deck_that_does_not_exist() -> tuple[bytes, dict[str, str]]:
    lobby, _private = an_instance()
    answer = ask_about(lobby, "ghost")
    assert answer.status_code == HTTPStatus.NOT_FOUND
    return answer.content, dict(answer.headers)


def test_the_owner_asks_about_their_own_built_deck() -> None:
    """E11 (#355) matrix, deck: the owner's own built deck is answered."""
    lobby, private = an_instance()

    answer = ask(lobby.client)

    assert answer.status_code == HTTPStatus.OK
    assert [question.deck for question in private.questions] == [ASKED]


@pytest.mark.parametrize(
    ("others", "removed", "slug"),
    [
        pytest.param((UNBUILT,), (), TARGET, id="own and unbuilt"),
        pytest.param((BUILT_TARGET,), (TARGET,), TARGET, id="removed"),
        pytest.param((ANOTHERS,), (), TARGET, id="another admin's"),
        pytest.param((), (), TARGET, id="nonexistent"),
        pytest.param((SOURCELESS,), (), TARGET, id="source gone"),
        pytest.param((), (), "..", id="dot dot"),
        pytest.param((), (), "a/b", id="path with a slash"),
        pytest.param((), (), "a\x00b", id="control character"),
        pytest.param((), (), "x" * 255, id="at the length bound"),
    ],
)
def test_a_refused_deck_gets_one_answer_and_spends_no_question(
    others: tuple[Deck, ...], removed: tuple[str, ...], slug: str
) -> None:
    """E11 (#355) matrix, deck: every unusable deck gets one 404, no port, no budget."""
    lobby, private = an_instance(*others, removed=removed)

    refused = ask_about(lobby, slug)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert (refused.content, dict(refused.headers)) == (
        the_answer_to_a_deck_that_does_not_exist()
    )
    assert private.questions == []
    assert ask(lobby.client).status_code == HTTPStatus.OK


def test_a_question_without_a_deck_gets_the_same_refusal() -> None:
    """E11 (#355) matrix, deck: a missing deck field gets the one 404."""
    lobby, private = an_instance()
    without_deck = {key: value for key, value in QUESTION.items() if key != "deck"}

    refused = ask(lobby.client, without_deck)

    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert (refused.content, dict(refused.headers)) == (
        the_answer_to_a_deck_that_does_not_exist()
    )
    assert private.questions == []
    assert ask(lobby.client).status_code == HTTPStatus.OK


def test_a_name_over_the_length_bound_is_rejected_before_any_lookup() -> None:
    """E11 (#355) matrix, deck: a name over 255 is 422 and spends no question."""
    lobby, private = an_instance()

    refused = ask_about(lobby, "x" * 256)

    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert private.questions == []
    assert ask(lobby.client).status_code == HTTPStatus.OK


def test_an_owner_who_lost_the_admin_role_is_refused() -> None:
    """E11 (#355) matrix, asker: #400 [Admin-Rolle verloren], 403, no budget."""
    users = FakeUserStore()
    lobby, private, _clock = a_copresenter_lobby(
        users=users,
        limiter=AskLimiter(clock=FrozenClock(instant=NOW), limits=ONE_QUESTION_ONLY),
    )
    lobby.sign_in(role=Role.ADMIN)
    users.users[ADMIN] = replace(users.users[ADMIN], role=Role.USER)

    refused = ask(lobby.client)
    users.users[ADMIN] = replace(users.users[ADMIN], role=Role.ADMIN)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert private.questions == []
    assert ask(lobby.client).status_code == HTTPStatus.OK
    assert len(private.questions) == 1


def test_a_signed_out_asker_is_refused_and_spends_no_question() -> None:
    """E11 (#355) matrix, asker: signed out is refused 403, no port, no budget."""
    lobby, private, _clock = a_copresenter_lobby(
        limiter=AskLimiter(clock=FrozenClock(instant=NOW), limits=ONE_QUESTION_ONLY),
    )

    lobby.sign_in(role=Role.ADMIN)
    session = lobby.client.cookies[DEFAULT_SESSION_COOKIE_NAME]
    lobby.client.cookies.delete(DEFAULT_SESSION_COOKIE_NAME)

    refused = ask(lobby.client)
    lobby.client.cookies.set(DEFAULT_SESSION_COOKIE_NAME, session)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert private.questions == []
    assert ask(lobby.client).status_code == HTTPStatus.OK
