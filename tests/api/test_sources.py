"""Settings · Sources, driven the way a browser drives them."""

from datetime import timedelta
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.api.preferences import SETTINGS
from presentator.api.sources import SOURCES
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    DeckFolder,
    Source,
    SourceRun,
    SourceRunFailure,
    SourceRunOutcome,
)
from presentator.contracts.models import Role
from tests.api.lobby import (
    ADMIN,
    ENGLISH,
    NEIGHBOUR,
    NO_DECKS,
    NOW,
    USERNAME,
    GivenDecks,
    Lobby,
    a_configured_source,
    a_lobby,
    a_signed_in_lobby,
    a_user_store,
    an_account,
)

_ADDRESS = "git@heimserver:decks.git"
_OTHER_ADDRESS = "https://gitlab.example.invalid/decks.git"
_OTHER_ID = "the-other-source"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_A_DECK = frozenset({MANIFEST_FILE, SLIDES_FILE})
_FETCH = f"{SOURCES}/{{name}}/fetch"
_NEVER_AN_AGE = "—"


def another_source() -> Source:
    return Source(
        id=_OTHER_ID,
        name="talks",
        url=_OTHER_ADDRESS,
        ref="main",
        secret_location=None,
        owner_id=ADMIN,
    )


def a_folder() -> DeckFolder:
    return DeckFolder(
        name="kundenfeedback",
        file_names=_A_DECK,
        title="A talk",
        changed_at=NOW - timedelta(minutes=2),
        commit=_COMMIT,
    )


def a_run(
    source_id: str,
    *,
    ago: timedelta,
    outcome: SourceRunOutcome,
    reason: SourceRunFailure | None = None,
) -> SourceRun:
    return SourceRun(
        source_id=source_id,
        at=NOW - ago,
        outcome=outcome,
        commit=None if outcome is SourceRunOutcome.FAILURE else _COMMIT,
        reason=reason,
    )


def an_instance_of_two_people(*, given: GivenDecks = NO_DECKS) -> Lobby:
    lobby = a_lobby(
        users=a_user_store(
            an_account(USERNAME, role=Role.ADMIN),
            an_account(NEIGHBOUR),
        ),
        given=given,
    )
    lobby.log_in()
    return lobby


def signed_in_as(lobby: Lobby, username: str) -> None:
    lobby.client.post("/logout")
    lobby.log_in(username=username)


def fetch(
    client: TestClient,
    name: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return client.post(_FETCH.format(name=name), headers=headers)


@pytest.fixture
def instance() -> Lobby:
    return an_instance_of_two_people(
        given=GivenDecks(source=a_configured_source(_ADDRESS)),
    )


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_person_without_the_admin_role_is_refused_at_sources(
    instance: Lobby,
    method: str,
) -> None:
    signed_in_as(instance, NEIGHBOUR)

    refused = (
        instance.client.get(SOURCES)
        if method == "get"
        else fetch(instance.client, "decks")
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_only_an_admin_is_offered_the_settings_section(instance: Lobby) -> None:
    assert ENGLISH.section_settings in instance.client.get("/").text

    signed_in_as(instance, NEIGHBOUR)

    assert ENGLISH.section_settings not in instance.client.get("/").text


def test_the_settings_link_is_current_on_the_sources_address(instance: Lobby) -> None:
    page = instance.client.get(SOURCES).text

    assert f'aria-current="page">{ENGLISH.section_settings}<' in page
    assert f'href="{SOURCES}" aria-current="page">{ENGLISH.settings_sources}<' in page
    assert f'href="{SETTINGS}">{ENGLISH.settings_title}<' in page


def test_settings_general_and_sources_share_the_tab_strip(instance: Lobby) -> None:
    general = instance.client.get(SETTINGS).text

    assert f'href="{SETTINGS}" aria-current="page">{ENGLISH.settings_title}<' in general
    assert f'href="{SOURCES}">{ENGLISH.settings_sources}<' in general


def test_a_source_with_no_run_reads_never_fetched_with_an_em_dash_for_the_age() -> None:
    page = (
        a_signed_in_lobby(
            GivenDecks(source=a_configured_source(_ADDRESS)),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.source_state_never_fetched in page
    assert _NEVER_AN_AGE in page
    assert 'data-state="never-fetched"' in page
    assert "decks" in page
    assert _ADDRESS in page
    assert ENGLISH.sources_fetch_now in page
    assert ENGLISH.decks_column_state in page
    assert ENGLISH.sources_column_source in page
    assert ENGLISH.sources_column_fetched in page


def test_a_row_carries_the_state_and_the_fetched_age_of_the_newest_run() -> None:
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(minutes=3),
                        outcome=SourceRunOutcome.SUCCESS,
                    ),
                ),
            ),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.source_state_reachable in page
    assert 'data-state="reachable"' in page
    assert "3 minutes ago" in page
    assert ENGLISH.source_state_never_fetched not in page


def test_a_failed_newest_run_reads_error_and_never_the_credential_word() -> None:
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(hours=2),
                        outcome=SourceRunOutcome.FAILURE,
                        reason=SourceRunFailure.CREDENTIAL_UNRESOLVABLE,
                    ),
                ),
            ),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.source_state_error in page
    assert 'data-state="error"' in page
    assert "2 hours ago" in page
    assert SourceRunFailure.CREDENTIAL_UNRESOLVABLE.value not in page


def test_fetch_now_refreshes_that_one_source_and_returns_to_the_list() -> None:
    configured = a_configured_source(_ADDRESS)
    other = another_source()
    lobby = a_signed_in_lobby(
        GivenDecks(
            sources=(configured, other),
            carried={configured.id: (a_folder(),), other.id: (a_folder(),)},
            runs=(
                a_run(
                    other.id,
                    ago=timedelta(minutes=3),
                    outcome=SourceRunOutcome.SUCCESS,
                ),
            ),
        ),
    )

    fetched = fetch(lobby, configured.name)

    assert fetched.status_code == HTTPStatus.SEE_OTHER
    assert fetched.headers["location"] == SOURCES
    page = lobby.get(SOURCES).text
    reachable_at = page.index(ENGLISH.source_state_reachable)
    never_at = page.index("3 minutes ago")
    assert page.index("decks") < page.index("talks")
    assert reachable_at < never_at
    assert ENGLISH.source_state_never_fetched not in page
    assert _NEVER_AN_AGE not in page


def test_the_empty_state_renders_when_no_source_exists() -> None:
    page = a_signed_in_lobby().get(SOURCES).text

    assert ENGLISH.sources_empty_title in page
    assert ENGLISH.sources_empty_explanation in page
    assert ENGLISH.sources_empty_need in page
    assert ENGLISH.sources_empty_ssh in page
    assert ENGLISH.sources_empty_or in page
    assert ENGLISH.sources_empty_https in page
    assert "<table" not in page
    assert ENGLISH.sources_fetch_now not in page


def test_fetch_now_of_a_name_this_instance_does_not_have_is_not_found() -> None:
    refused = fetch(a_signed_in_lobby(), "no-such-source")

    assert refused.status_code == HTTPStatus.NOT_FOUND


def test_fetch_now_from_another_site_is_refused(instance: Lobby) -> None:
    refused = fetch(
        instance.client,
        "decks",
        headers={"origin": "https://another.example"},
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_a_visitor_who_is_not_signed_in_is_sent_to_the_login() -> None:
    lobby = a_lobby(given=GivenDecks(source=a_configured_source(_ADDRESS)))

    reading = lobby.client.get(SOURCES)
    posting = fetch(lobby.client, "decks")

    assert reading.status_code == HTTPStatus.FOUND
    assert reading.headers["location"] == "/login"
    assert posting.status_code == HTTPStatus.FOUND
    assert posting.headers["location"] == "/login"
