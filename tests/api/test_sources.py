"""Settings · Sources, driven the way a browser drives them."""

import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.api.hooks import hook_address
from presentator.api.preferences import SETTINGS
from presentator.api.sources import (
    ACCESS,
    NEW,
    REMOVE,
    REMOVE_CONFIRMED,
    SOURCES,
    WEBHOOK,
)
from presentator.contracts.decks import (
    MANIFEST_FILE,
    SLIDES_FILE,
    Build,
    Deck,
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
    TYPED_WORDS,
    USERNAME,
    GivenDecks,
    Lobby,
    a_configured_source,
    a_lobby,
    a_lobby_app,
    a_signed_in_lobby,
    a_user_store,
    an_account,
)
from tests.application.fakes import LOCAL_MOUNT_EXAMPLE, FakeDeckStore

_ADDRESS = "git@heimserver:decks.git"
_OTHER_ADDRESS = "https://gitlab.example.invalid/decks.git"
_OTHER_ID = "the-other-source"
_COMMIT = "a3f19c2b8d4e5f60718293a4b5c6d7e8f9012345"
_A_DECK = frozenset({MANIFEST_FILE, SLIDES_FILE})
_FETCH = f"{SOURCES}/{{name}}/fetch"
_NEVER_AN_AGE = "—"
_HTTPS_URL = "https://git.example.invalid/talks.git"
_READ_ONLY = "a-read-only-token"
_SHORT_ACCESS = "zz-short-token"
_LONG_ACCESS = f"long-{_READ_ONLY}-and-then-some"
_SECRET_DOT_ROWS = 2
_HOOK_SECRET = re.compile(r"data-hook-secret>([^<]+)<")


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


def ask_removal(
    client: TestClient,
    name: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return client.post(REMOVE.format(name=name), headers=headers)


def confirm_removal(
    client: TestClient,
    name: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return client.post(REMOVE_CONFIRMED.format(name=name), headers=headers)


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


def test_a_refused_login_reads_refused_rather_than_error() -> None:
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(minutes=5),
                        outcome=SourceRunOutcome.FAILURE,
                        reason=SourceRunFailure.REFUSED,
                    ),
                ),
            ),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.source_state_refused in page
    assert 'data-state="refused"' in page
    assert ENGLISH.source_state_error not in page


def test_a_host_that_answered_with_something_else_reads_failed_rather_than_error() -> (
    None
):
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(minutes=9),
                        outcome=SourceRunOutcome.FAILURE,
                        reason=SourceRunFailure.FAILED,
                    ),
                ),
            ),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.source_state_failed in page
    assert 'data-state="failed"' in page
    assert ENGLISH.source_state_error not in page


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


def create_source(
    client: TestClient,
    *,
    name: str = "talks",
    url: str = _HTTPS_URL,
    access: str = "https",
    secret: str = _READ_ONLY,
) -> Response:
    return client.post(
        NEW,
        data={"name": name, "url": url, "access": access, "secret": secret},
    )


def the_created_page(client: TestClient, created: Response) -> Response:
    assert created.status_code == HTTPStatus.SEE_OTHER
    return client.get(created.headers["location"])


def two_admin_sessions() -> tuple[TestClient, TestClient]:
    """Two admin sessions over one app, each with its own cookie jar."""
    app, _ = a_lobby_app(
        users=a_user_store(
            an_account(USERNAME, role=Role.ADMIN),
            an_account(NEIGHBOUR, role=Role.ADMIN),
        ),
    )
    creator = TestClient(app, follow_redirects=False)
    other = TestClient(app, follow_redirects=False)
    creator.post("/login", data={"username": USERNAME, "password": TYPED_WORDS})
    other.post("/login", data={"username": NEIGHBOUR, "password": TYPED_WORDS})
    return creator, other


def a_one_time_secret(operation: str, creator: TestClient) -> Response:
    """Mint a one-time secret, then the redirect that carries it to the page."""
    if operation == "create":
        return create_source(creator, name="talks")
    created = create_source(creator, name="talks")
    the_created_page(creator, created)
    return creator.post(WEBHOOK.format(name="talks"))


def test_the_list_and_the_empty_state_offer_add_source() -> None:
    empty = a_signed_in_lobby().get(SOURCES).text
    filled = (
        a_signed_in_lobby(GivenDecks(source=a_configured_source(_ADDRESS)))
        .get(SOURCES)
        .text
    )

    assert f'href="{NEW}">{ENGLISH.sources_add}<' in empty
    assert f'href="{NEW}">{ENGLISH.sources_add}<' in filled


def test_the_access_column_names_how_the_url_is_read() -> None:
    page = (
        a_signed_in_lobby(
            GivenDecks(source=a_configured_source(_ADDRESS)),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.sources_column_access in page
    assert ENGLISH.source_access_deploy_key in page


def test_only_an_admin_reaches_the_add_form(instance: Lobby) -> None:
    signed_in_as(instance, NEIGHBOUR)

    assert instance.client.get(NEW).status_code == HTTPStatus.FORBIDDEN
    assert create_source(instance.client).status_code == HTTPStatus.FORBIDDEN


def test_only_an_admin_reaches_the_created_page(instance: Lobby) -> None:
    created = create_source(instance.client)
    signed_in_as(instance, NEIGHBOUR)

    assert instance.client.get(created.headers["location"]).status_code == (
        HTTPStatus.FORBIDDEN
    )


def test_a_created_page_for_a_name_this_instance_does_not_have_is_not_found() -> None:
    assert (
        a_signed_in_lobby().get(f"{SOURCES}/talks").status_code == HTTPStatus.NOT_FOUND
    )


def test_a_url_that_names_no_access_kind_has_no_access_tag() -> None:
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=a_configured_source("http://git.example.invalid/decks.git")
            ),
        )
        .get(SOURCES)
        .text
    )

    assert ENGLISH.source_access_token not in page
    assert ENGLISH.source_access_deploy_key not in page
    assert ENGLISH.source_access_local not in page


def test_the_add_form_ships_https_live_and_ssh_and_check_disabled() -> None:
    page = a_signed_in_lobby().get(NEW).text

    assert ENGLISH.sources_add in page
    assert ENGLISH.source_name in page
    assert ENGLISH.source_url in page
    assert ENGLISH.source_access_https in page
    assert ENGLISH.source_access_ssh in page
    assert 'name="access" value="ssh" disabled' in page
    assert 'name="access" value="https"' in page
    assert "checked" in page
    assert ENGLISH.source_check in page
    assert f">{ENGLISH.source_check}<" in page
    assert "disabled" in page
    assert ENGLISH.source_create in page
    assert 'name="secret"' in page
    assert 'value="' not in page.split('name="secret"')[1].split(">")[0]


def test_the_add_form_offers_a_folder_on_this_box_option() -> None:
    page = a_signed_in_lobby().get(NEW).text

    assert ENGLISH.source_access_file in page
    assert 'name="access" value="file"' in page
    assert 'name="access" value="file" disabled' not in page


def test_creating_a_source_on_this_box_accepts_a_file_address_under_the_mount() -> None:
    lobby = a_signed_in_lobby()
    url = f"{LOCAL_MOUNT_EXAMPLE}/talks.git"

    the_created_page(
        lobby,
        create_source(lobby, url=url, access="file", secret=""),
    )
    listed = lobby.get(SOURCES).text

    assert "talks" in listed
    assert url in listed
    assert ENGLISH.source_access_local in listed


def test_a_secret_is_refused_for_a_source_on_this_box() -> None:
    lobby = a_signed_in_lobby()
    url = f"{LOCAL_MOUNT_EXAMPLE}/talks.git"

    refused = create_source(lobby, url=url, access="file", secret=_READ_ONLY)

    assert ENGLISH.source_refused_secret_not_allowed in refused.text
    assert _READ_ONLY not in refused.text


def test_a_file_address_outside_the_mount_is_refused() -> None:
    lobby = a_signed_in_lobby()

    refused = create_source(
        lobby,
        url="/etc/talks.git",
        access="file",
        secret="",
    )

    assert ENGLISH.source_refused_outside_mount in refused.text


def test_creating_a_source_stores_it_fetches_it_and_shows_address_and_secret() -> None:
    lobby = a_signed_in_lobby()

    page = the_created_page(lobby, create_source(lobby)).text
    secret = _HOOK_SECRET.search(page)

    assert secret is not None
    assert secret.group(1) != _READ_ONLY
    assert hook_address("talks") in page
    assert ENGLISH.source_webhook_once in page
    assert ENGLISH.source_created_toast in page
    listed = lobby.get(SOURCES).text
    assert "talks" in listed
    assert _HTTPS_URL in listed
    assert ENGLISH.source_state_reachable in listed
    assert ENGLISH.source_access_token in listed


def test_a_second_load_of_the_created_page_shows_the_secret_no_more() -> None:
    lobby = a_signed_in_lobby()
    created = create_source(lobby)
    first = the_created_page(lobby, created)
    secret = _HOOK_SECRET.search(first.text)
    assert secret is not None

    second = lobby.get(created.headers["location"]).text

    assert secret.group(1) in first.text
    assert secret.group(1) not in second
    assert ENGLISH.source_webhook_once not in second
    assert hook_address("talks") in second


@pytest.mark.parametrize("operation", ["create", "renew"])
def test_the_once_shown_secret_is_bound_to_the_session_that_asked(
    operation: str,
) -> None:
    creator, other = two_admin_sessions()
    minted = a_one_time_secret(operation, creator)

    assert minted.status_code == HTTPStatus.SEE_OTHER
    first = creator.get(minted.headers["location"]).text
    secret = _HOOK_SECRET.search(first)
    assert secret is not None
    assert secret.group(1) != _READ_ONLY
    assert ENGLISH.source_webhook_once in first
    assert _HOOK_SECRET.search(other.get(minted.headers["location"]).text) is None
    reloaded = creator.get(minted.headers["location"]).text
    assert secret.group(1) not in reloaded
    assert _HOOK_SECRET.search(other.get(minted.headers["location"]).text) is None


@pytest.mark.parametrize("operation", ["create", "renew"])
def test_a_different_session_sees_the_held_sentence_and_no_secret(
    operation: str,
) -> None:
    creator, other = two_admin_sessions()
    minted = a_one_time_secret(operation, creator)

    page = other.get(minted.headers["location"]).text
    assert _HOOK_SECRET.search(page) is None
    assert "data-hook-secret" not in page
    assert ENGLISH.source_webhook_held_elsewhere in page
    assert ENGLISH.source_secret_dots in page
    assert ENGLISH.source_created_toast not in page
    creator.get(minted.headers["location"])
    after = other.get(minted.headers["location"]).text
    assert ENGLISH.source_webhook_held_elsewhere not in after
    assert "data-hook-secret" not in after


def test_a_call_to_the_shown_address_with_the_shown_secret_answers_204() -> None:
    lobby = a_signed_in_lobby()
    page = the_created_page(lobby, create_source(lobby, name="alpha")).text
    secret = _HOOK_SECRET.search(page)
    assert secret is not None

    called = lobby.post(
        hook_address("alpha"),
        headers={"authorization": f"Bearer {secret.group(1)}"},
    )
    gitlab = lobby.post(
        hook_address("alpha"),
        headers={"x-gitlab-token": secret.group(1)},
    )

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert gitlab.status_code == HTTPStatus.NO_CONTENT


def test_an_existing_source_without_a_stored_secret_stays_when_another_is_added() -> (
    None
):
    existing = a_configured_source(_ADDRESS)
    lobby = a_signed_in_lobby(GivenDecks(source=existing))

    the_created_page(lobby, create_source(lobby))
    listed = lobby.get(SOURCES).text

    assert "decks" in listed
    assert _ADDRESS in listed
    assert ENGLISH.source_access_deploy_key in listed
    assert "talks" in listed


@pytest.mark.parametrize(
    ("fields", "sentence"),
    [
        pytest.param(
            {
                "name": "Talks",
                "url": _HTTPS_URL,
                "access": "https",
                "secret": _READ_ONLY,
            },
            ENGLISH.source_refused_name,
            id="malformed name",
        ),
        pytest.param(
            {"name": "new", "url": _HTTPS_URL, "access": "https", "secret": _READ_ONLY},
            ENGLISH.source_refused_name,
            id="reserved name",
        ),
        pytest.param(
            {
                "name": "talks",
                "url": "https://user:token@git.example.invalid/talks.git",
                "access": "https",
                "secret": _READ_ONLY,
            },
            ENGLISH.source_refused_password,
            id="password in the URL",
        ),
        pytest.param(
            {
                "name": "talks",
                "url": "git@git.example.invalid:talks.git",
                "access": "https",
                "secret": _READ_ONLY,
            },
            ENGLISH.source_refused_access,
            id="scheme mismatch",
        ),
        pytest.param(
            {
                "name": "talks",
                "url": "http://git.example.invalid/talks.git",
                "access": "https",
                "secret": _READ_ONLY,
            },
            ENGLISH.source_refused_access,
            id="http URL",
        ),
        pytest.param(
            {
                "name": "talks",
                "url": _HTTPS_URL,
                "access": "ssh",
                "secret": _READ_ONLY,
            },
            ENGLISH.source_refused_access,
            id="ssh radio",
        ),
        pytest.param(
            {"name": "talks", "url": _HTTPS_URL, "access": "https", "secret": ""},
            ENGLISH.source_refused_secret,
            id="missing secret",
        ),
    ],
)
def test_a_refused_form_comes_back_without_the_secret(
    fields: dict[str, str],
    sentence: str,
) -> None:
    lobby = a_signed_in_lobby()

    refused = create_source(lobby, **fields)

    assert refused.status_code == HTTPStatus.OK
    assert sentence in refused.text
    assert 'name="secret"' in refused.text
    secret_input = refused.text.split('name="secret"')[1].split(">")[0]
    assert "value=" not in secret_input
    assert _READ_ONLY not in refused.text
    assert ENGLISH.sources_empty_title in lobby.get(SOURCES).text


def test_a_duplicate_name_or_url_is_refused() -> None:
    lobby = a_signed_in_lobby()
    the_created_page(lobby, create_source(lobby))

    duplicate_name = create_source(lobby, url="https://git.example.invalid/other.git")
    duplicate_url = create_source(lobby, name="other")

    assert ENGLISH.source_refused_duplicate_name in duplicate_name.text
    assert ENGLISH.source_refused_duplicate_url in duplicate_url.text
    assert _READ_ONLY not in duplicate_name.text
    assert _READ_ONLY not in duplicate_url.text


def test_add_source_from_another_site_is_refused() -> None:
    lobby = a_signed_in_lobby()

    refused = lobby.post(
        NEW,
        data={
            "name": "talks",
            "url": _HTTPS_URL,
            "access": "https",
            "secret": _READ_ONLY,
        },
        headers={"origin": "https://another.example"},
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_a_visitor_who_is_not_signed_in_cannot_add_a_source() -> None:
    lobby = a_lobby()

    reading = lobby.client.get(NEW)
    posting = create_source(lobby.client)

    assert reading.status_code == HTTPStatus.FOUND
    assert reading.headers["location"] == "/login"
    assert posting.status_code == HTTPStatus.FOUND
    assert posting.headers["location"] == "/login"


def a_deck_from(source: Source) -> Deck:
    return Deck(
        slug="kundenfeedback",
        title="Kundenfeedback Q3",
        changed_at=NOW - timedelta(minutes=2),
        owner_id=ADMIN,
        source_id=source.id,
        commit=_COMMIT,
        build=None,
        attempt=None,
    )


def test_the_source_page_shows_state_newest_runs_and_the_decks_from_here() -> None:
    source = a_configured_source(_ADDRESS)
    store = FakeDeckStore()
    store.put(a_deck_from(source))
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                store=store,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(minutes=13),
                        outcome=SourceRunOutcome.FAILURE,
                        reason=SourceRunFailure.UNREACHABLE,
                    ),
                    a_run(
                        source.id,
                        ago=timedelta(minutes=8),
                        outcome=SourceRunOutcome.SUCCESS,
                    ),
                    a_run(
                        source.id,
                        ago=timedelta(minutes=3),
                        outcome=SourceRunOutcome.SUCCESS,
                    ),
                ),
            ),
        )
        .get(f"{SOURCES}/{source.name}")
        .text
    )

    assert source.name in page
    assert _ADDRESS in page
    assert ENGLISH.source_state_reachable in page
    assert "3 minutes ago" in page
    assert _COMMIT[:7] in page
    assert ENGLISH.source_run_unreachable in page
    assert ENGLISH.source_run_column_state in page
    assert ENGLISH.source_run_column_fetched in page
    assert ENGLISH.source_run_column_detail in page
    assert "Kundenfeedback Q3" in page
    assert "kundenfeedback" in page
    assert ENGLISH.sources_fetch_now in page
    assert ENGLISH.source_renew in page
    assert ENGLISH.source_later in page
    assert "data-later" in page
    assert ENGLISH.source_secret_dots in page
    assert "Built isolated" not in page


def test_a_refused_run_on_the_source_page_reads_refused_not_error() -> None:
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(minutes=5),
                        outcome=SourceRunOutcome.FAILURE,
                        reason=SourceRunFailure.REFUSED,
                    ),
                ),
            ),
        )
        .get(f"{SOURCES}/{source.name}")
        .text
    )

    assert ENGLISH.source_state_refused in page
    assert ENGLISH.source_run_refused in page
    assert 'data-state="refused"' in page


def test_a_failed_run_on_the_source_page_reads_failed_not_error() -> None:
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(
            GivenDecks(
                source=source,
                runs=(
                    a_run(
                        source.id,
                        ago=timedelta(minutes=9),
                        outcome=SourceRunOutcome.FAILURE,
                        reason=SourceRunFailure.FAILED,
                    ),
                ),
            ),
        )
        .get(f"{SOURCES}/{source.name}")
        .text
    )

    assert ENGLISH.source_state_failed in page
    assert ENGLISH.source_run_failed in page
    assert 'data-state="failed"' in page


def test_a_source_without_a_stored_secret_says_so_on_its_page() -> None:
    source = a_configured_source(_ADDRESS)
    page = (
        a_signed_in_lobby(GivenDecks(source=source))
        .get(f"{SOURCES}/{source.name}")
        .text
    )

    assert ENGLISH.source_secret_missing in page
    assert "data-secret-missing" in page
    assert ENGLISH.source_secret_dots in page


def test_a_source_on_this_box_offers_neither_dots_nor_renew_on_its_page() -> None:
    lobby = a_signed_in_lobby()
    url = f"{LOCAL_MOUNT_EXAMPLE}/talks.git"

    the_created_page(lobby, create_source(lobby, url=url, access="file", secret=""))
    page = lobby.get(f"{SOURCES}/talks").text

    assert "data-renew-access" not in page
    assert ENGLISH.source_access_local in page


def test_fetch_now_from_the_source_page_refreshes_it_and_stays() -> None:
    configured = a_configured_source(_ADDRESS)
    lobby = a_signed_in_lobby(
        GivenDecks(
            sources=(configured, another_source()),
            carried={configured.id: (a_folder(),), _OTHER_ID: (a_folder(),)},
        ),
    )

    fetched = lobby.post(
        _FETCH.format(name=configured.name),
        data={"stay": "page"},
    )

    assert fetched.status_code == HTTPStatus.SEE_OTHER
    assert fetched.headers["location"] == f"{SOURCES}/{configured.name}"
    page = lobby.get(f"{SOURCES}/{configured.name}").text
    assert ENGLISH.source_state_reachable in page
    assert _COMMIT[:7] in page


def test_renewing_the_webhook_secret_shows_it_once_and_retires_the_old_one() -> None:
    lobby = a_signed_in_lobby()
    created = create_source(lobby, name="alpha")
    first = the_created_page(lobby, created)
    old_secret = _HOOK_SECRET.search(first.text)
    assert old_secret is not None
    lobby.get(created.headers["location"])

    renewed = lobby.post(WEBHOOK.format(name="alpha"))
    assert renewed.status_code == HTTPStatus.SEE_OTHER
    shown = lobby.get(renewed.headers["location"])
    new_secret = _HOOK_SECRET.search(shown.text)
    assert new_secret is not None
    assert new_secret.group(1) != old_secret.group(1)
    assert ENGLISH.source_webhook_once in shown.text
    reloaded = lobby.get(renewed.headers["location"]).text
    assert new_secret.group(1) not in reloaded
    assert ENGLISH.source_webhook_once not in reloaded

    old_call = lobby.post(
        hook_address("alpha"),
        headers={"authorization": f"Bearer {old_secret.group(1)}"},
    )
    new_call = lobby.post(
        hook_address("alpha"),
        headers={"authorization": f"Bearer {new_secret.group(1)}"},
    )
    assert old_call.status_code == HTTPStatus.NOT_FOUND
    assert new_call.status_code == HTTPStatus.NO_CONTENT


def test_renewing_the_access_secret_takes_a_value_and_shows_nothing_back() -> None:
    lobby = a_signed_in_lobby()
    the_created_page(lobby, create_source(lobby))
    rotated = f"rotated-{_READ_ONLY}"

    renewed = lobby.post(
        ACCESS.format(name="talks"),
        data={"secret": rotated},
    )

    assert renewed.status_code == HTTPStatus.SEE_OTHER
    page = lobby.get(renewed.headers["location"]).text
    assert rotated not in page
    assert _READ_ONLY not in page
    assert ENGLISH.source_secret_dots in page
    assert page.count(ENGLISH.source_secret_dots) == _SECRET_DOT_ROWS


def test_a_blank_access_renewal_comes_back_without_storing_and_without_the_value() -> (
    None
):
    lobby = a_signed_in_lobby()
    the_created_page(lobby, create_source(lobby))

    refused = lobby.post(ACCESS.format(name="talks"), data={"secret": "  "})

    assert refused.status_code == HTTPStatus.OK
    assert ENGLISH.source_refused_secret in refused.text
    assert _READ_ONLY not in refused.text


def test_renewing_the_access_secret_is_refused_for_a_source_on_this_box() -> None:
    lobby = a_signed_in_lobby()
    url = f"{LOCAL_MOUNT_EXAMPLE}/talks.git"
    the_created_page(
        lobby,
        create_source(lobby, url=url, access="file", secret=""),
    )

    refused = lobby.post(ACCESS.format(name="talks"), data={"secret": _READ_ONLY})

    assert refused.status_code == HTTPStatus.OK
    assert _READ_ONLY not in refused.text
    page = lobby.get(f"{SOURCES}/talks").text
    assert "data-renew-access" not in page


def test_the_source_page_never_derives_dot_count_from_a_secret() -> None:
    lobby = a_signed_in_lobby()
    the_created_page(lobby, create_source(lobby, name="short", secret=_SHORT_ACCESS))
    the_created_page(
        lobby,
        create_source(
            lobby,
            name="long",
            url="https://git.example.invalid/long.git",
            secret=_LONG_ACCESS,
        ),
    )
    short = lobby.get(f"{SOURCES}/short").text
    long = lobby.get(f"{SOURCES}/long").text

    assert short.count(ENGLISH.source_secret_dots) == long.count(
        ENGLISH.source_secret_dots,
    )
    assert _SHORT_ACCESS not in short
    assert _LONG_ACCESS not in long


@pytest.mark.parametrize("method", ["access", "webhook"])
def test_only_an_admin_renews_either_secret(instance: Lobby, method: str) -> None:
    created = create_source(instance.client)
    the_created_page(instance.client, created)
    signed_in_as(instance, NEIGHBOUR)
    path = (ACCESS if method == "access" else WEBHOOK).format(name="talks")

    refused = instance.client.post(path, data={"secret": _READ_ONLY})

    assert refused.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("method", ["access", "webhook", "fetch"])
def test_source_page_posts_from_another_site_are_refused(
    instance: Lobby,
    method: str,
) -> None:
    created = create_source(instance.client)
    the_created_page(instance.client, created)
    path = {
        "access": ACCESS.format(name="talks"),
        "webhook": WEBHOOK.format(name="talks"),
        "fetch": _FETCH.format(name="talks"),
    }[method]

    refused = instance.client.post(
        path,
        data={"secret": _READ_ONLY, "stay": "page"},
        headers={"origin": "https://another.example"},
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN


def test_renewing_a_name_this_instance_does_not_have_is_not_found() -> None:
    lobby = a_signed_in_lobby()

    assert (
        lobby.post(
            ACCESS.format(name="missing"),
            data={"secret": _READ_ONLY},
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    missing = lobby.post(WEBHOOK.format(name="missing"))
    assert missing.status_code == HTTPStatus.NOT_FOUND


def test_the_list_links_each_name_to_its_page() -> None:
    page = (
        a_signed_in_lobby(GivenDecks(source=a_configured_source(_ADDRESS)))
        .get(SOURCES)
        .text
    )

    assert f'href="{SOURCES}/decks"' in page


def test_the_source_page_offers_a_remove_control_at_the_bottom() -> None:
    page = (
        a_signed_in_lobby(GivenDecks(source=a_configured_source(_ADDRESS)))
        .get(f"{SOURCES}/decks")
        .text
    )

    assert ENGLISH.source_remove_heading in page
    assert ENGLISH.source_remove_hint in page
    assert f'action="{SOURCES}/decks/remove"' in page
    assert ENGLISH.source_remove_button in page


def test_asking_to_remove_a_source_shows_the_confirm_and_deletes_nothing() -> None:
    source = a_configured_source(_ADDRESS)
    lobby = a_signed_in_lobby(GivenDecks(source=source, folders=(a_folder(),)))

    asked = ask_removal(lobby, source.name)

    assert asked.status_code == HTTPStatus.OK
    assert ENGLISH.source_remove_confirm_title.format(name=source.name) in asked.text
    assert ENGLISH.source_remove_confirm_body.format(decks=1, runs=1) in asked.text
    assert ENGLISH.source_remove_cancel in asked.text
    assert ENGLISH.source_remove_confirm in asked.text
    assert f'href="{SOURCES}/{source.name}"' in lobby.get(SOURCES).text


def test_confirming_removal_deletes_the_source_and_notices_the_list() -> None:
    source = a_configured_source(_ADDRESS)
    lobby = a_signed_in_lobby(GivenDecks(source=source, folders=(a_folder(),)))

    confirmed = confirm_removal(lobby, source.name)

    assert confirmed.status_code == HTTPStatus.SEE_OTHER
    assert confirmed.headers["location"] == SOURCES
    listed = lobby.get(SOURCES).text
    assert f'href="{SOURCES}/{source.name}"' not in listed
    assert ENGLISH.source_removed.format(name=source.name, decks=1, runs=1) in listed
    assert lobby.get(f"{SOURCES}/{source.name}").status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("step", ["ask", "confirm"])
def test_only_an_admin_may_remove_a_source(instance: Lobby, step: str) -> None:
    signed_in_as(instance, NEIGHBOUR)

    refused = (
        ask_removal(instance.client, "decks")
        if step == "ask"
        else confirm_removal(instance.client, "decks")
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("step", ["ask", "confirm"])
def test_removal_posts_from_another_site_are_refused(
    instance: Lobby,
    step: str,
) -> None:
    headers = {"origin": "https://another.example"}

    refused = (
        ask_removal(instance.client, "decks", headers=headers)
        if step == "ask"
        else confirm_removal(instance.client, "decks", headers=headers)
    )

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert f'href="{SOURCES}/decks"' in instance.client.get(SOURCES).text


@pytest.mark.parametrize("step", ["ask", "confirm"])
def test_removal_of_a_name_this_instance_does_not_have_is_not_found(step: str) -> None:
    lobby = a_signed_in_lobby()

    refused = (
        ask_removal(lobby, "no-such-source")
        if step == "ask"
        else confirm_removal(lobby, "no-such-source")
    )

    assert refused.status_code == HTTPStatus.NOT_FOUND


def test_after_removal_the_same_url_and_name_can_be_added_again() -> None:
    lobby = a_signed_in_lobby()
    created = create_source(lobby, name="talks", url=_HTTPS_URL)
    the_created_page(lobby, created)

    confirm_removal(lobby, "talks")
    re_created = create_source(lobby, name="talks", url=_HTTPS_URL)

    assert re_created.status_code == HTTPStatus.SEE_OTHER
    page = the_created_page(lobby, re_created).text
    assert ENGLISH.source_webhook_secret in page


def test_a_removed_decks_talk_answers_not_found(tmp_path: Path) -> None:
    source = a_configured_source(_ADDRESS)
    talk = tmp_path / "talk"
    talk.mkdir()
    (talk / "index.html").write_text("<html></html>", encoding="utf-8")
    store = FakeDeckStore()
    store.put(
        Deck(
            slug="kundenfeedback",
            title="A talk",
            changed_at=NOW,
            owner_id=ADMIN,
            source_id=source.id,
            commit=_COMMIT,
            build=None,
            attempt=None,
        ),
    )
    store.put_build(
        "kundenfeedback",
        Build(directory=talk, pdf=talk / "deck.pdf", commit=_COMMIT, built_at=NOW),
    )
    lobby = a_signed_in_lobby(GivenDecks(source=source, store=store))

    before = lobby.get("/deck/kundenfeedback/")
    confirm_removal(lobby, source.name)
    after = lobby.get("/deck/kundenfeedback/")

    assert before.status_code == HTTPStatus.OK
    assert after.status_code == HTTPStatus.NOT_FOUND
