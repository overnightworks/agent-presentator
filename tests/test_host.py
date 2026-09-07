"""The composition root, from the environment to a lobby that answers."""

import asyncio
import logging
import re
import shutil
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.adapters.decks import SqliteDeckStore
from presentator.adapters.identity import SqliteUserStore
from presentator.api.auth import SESSION_COOKIE
from presentator.api.hooks import HOOKS_PATH
from presentator.application.identity import FAILURES_BEFORE_THROTTLE
from presentator.host import main
from presentator.host.config import (
    SECRET_LENGTH,
    ConfigurationError,
    load_settings,
)
from presentator.host.main import Instance
from tests.conftest import EXAMPLE_SLUG, EXAMPLE_TITLE, GitRemote

_INSTANCE_KEY = "an instance key of at least thirty-two bytes"
_PERSON = "felix"
_TYPED_WORDS = "the words only this test types"
_PUSHED_AT = datetime(2026, 1, 15, 9, tzinfo=UTC)
_DELETED_AT = datetime(2026, 1, 16, 9, tzinfo=UTC)
_PUSHED_AGAIN_AT = datetime(2026, 2, 1, 9, tzinfo=UTC)
_ANOTHER_SLUG = "kundenfeedback"
_SOURCE_NAME = "talks"
_ANOTHER_SOURCE_NAME = "more-talks"
_WHAT_THE_HOST_CARRIES = "the words only this source's host was given"
_PEER = "127.0.0.1"
_FORWARDED_CLIENT = "203.0.113.10"
_ANOTHER_CLIENT = "198.51.100.20"
_WRONG_WORDS = "guessed"


@pytest.fixture
def bare_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRESENTATOR_SECRET_KEY", raising=False)
    monkeypatch.setenv("PRESENTATOR_DATABASE", str(tmp_path / "presentator.sqlite3"))
    return monkeypatch


@pytest.fixture
def environment(bare_environment: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    bare_environment.setenv("PRESENTATOR_SECRET_KEY", _INSTANCE_KEY)
    return bare_environment


def a_real_instance() -> Instance:
    """The whole stack the composition root builds, on the configured database."""
    return main.build_instance(load_settings())


def a_real_lobby() -> TestClient:
    """A browser at that stack."""
    return TestClient(a_real_instance().lobby, follow_redirects=False)


def signed_in(instance: Instance) -> TestClient:
    """A browser at that stack with the instance's admin created and signed in."""
    client = TestClient(instance.lobby, follow_redirects=False)
    client.post(
        "/setup",
        data={
            "username": _PERSON,
            "password": _TYPED_WORDS,
            "repeated_password": _TYPED_WORDS,
        },
    )
    return client


def test_the_composition_root_serves_a_lobby_that_answers_the_login(
    environment: pytest.MonkeyPatch,
) -> None:
    served: list[FastAPI] = []

    class ServerThatOnlyRecordsWhatItGot:
        def __init__(self, config: uvicorn.Config) -> None:
            assert isinstance(config.app, FastAPI)
            self.app = config.app

        async def serve(self) -> None:
            served.append(self.app)

    environment.setattr(main.uvicorn, "Server", ServerThatOnlyRecordsWhatItGot)

    main.main()

    assert len(served) == 1
    assert TestClient(served[0]).get("/login").is_success


def test_the_server_does_not_rewrite_the_client_from_forwarded_headers(
    environment: pytest.MonkeyPatch,
) -> None:
    recorded: list[uvicorn.Config] = []

    class ServerThatOnlyRecordsWhatItGot:
        def __init__(self, config: uvicorn.Config) -> None:
            recorded.append(config)

        async def serve(self) -> None:
            return

    environment.setattr(main.uvicorn, "Server", ServerThatOnlyRecordsWhatItGot)

    main.main()

    assert recorded[0].proxy_headers is False


def a_browser(instance: Instance, *, peer: str = _PEER) -> TestClient:
    """A browser whose ASGI peer is an address, so a proxy list can trust it."""
    return TestClient(instance.lobby, follow_redirects=False, client=(peer, 50000))


def an_admin_signed_out(instance: Instance) -> TestClient:
    """The real stack with its admin created and its session ended."""
    lobby = a_browser(instance)
    lobby.post(
        "/setup",
        data={
            "username": _PERSON,
            "password": _TYPED_WORDS,
            "repeated_password": _TYPED_WORDS,
        },
    )
    lobby.post("/logout")
    return lobby


def spend_the_address_budget(lobby: TestClient, *, forwarded: str) -> None:
    """Refuse under distinct names, so only this address is spent."""
    for index in range(FAILURES_BEFORE_THROTTLE):
        lobby.post(
            "/login",
            data={"username": f"nobody-{index}", "password": _WRONG_WORDS},
            headers={"x-forwarded-for": forwarded},
        )


@pytest.mark.usefixtures("environment")
def test_without_trusted_proxies_the_login_budget_keys_on_the_peer() -> None:
    lobby = an_admin_signed_out(a_real_instance())
    spend_the_address_budget(lobby, forwarded=_FORWARDED_CLIENT)

    still_the_peer = lobby.post(
        "/login",
        data={"username": _PERSON, "password": _TYPED_WORDS},
        headers={"x-forwarded-for": _ANOTHER_CLIENT},
    )

    assert still_the_peer.status_code == HTTPStatus.OK
    assert SESSION_COOKIE not in still_the_peer.cookies


def test_a_trusted_proxy_makes_the_forwarded_client_the_one_the_budget_counts(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("PRESENTATOR_TRUSTED_PROXIES", _PEER)
    lobby = an_admin_signed_out(a_real_instance())
    spend_the_address_budget(lobby, forwarded=_FORWARDED_CLIENT)

    same_client = lobby.post(
        "/login",
        data={"username": _PERSON, "password": _TYPED_WORDS},
        headers={"x-forwarded-for": _FORWARDED_CLIENT},
    )
    other_client = lobby.post(
        "/login",
        data={"username": _PERSON, "password": _TYPED_WORDS},
        headers={"x-forwarded-for": _ANOTHER_CLIENT},
    )

    assert same_client.status_code == HTTPStatus.OK
    assert other_client.status_code == HTTPStatus.SEE_OTHER


def test_a_trusted_proxy_list_that_is_not_addresses_refuses_to_start(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("PRESENTATOR_TRUSTED_PROXIES", "not-an-address")

    with pytest.raises(ConfigurationError, match="trusted_proxies"):
        load_settings()


@pytest.mark.usefixtures("environment")
def test_the_real_stack_signs_a_person_in_and_out_and_keeps_the_password_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_lobby = a_real_lobby()

    with caplog.at_level(logging.DEBUG):
        created = real_lobby.post(
            "/setup",
            data={
                "username": _PERSON,
                "password": _TYPED_WORDS,
                "repeated_password": _TYPED_WORDS,
            },
        )
        assert created.status_code == HTTPStatus.SEE_OTHER
        assert _PERSON in real_lobby.get("/").text

        signed_in_cookie = real_lobby.cookies[SESSION_COOKIE]
        assert real_lobby.post("/logout").status_code == HTTPStatus.SEE_OTHER

        real_lobby.cookies.set(SESSION_COOKIE, signed_in_cookie)
        assert real_lobby.get("/").status_code == HTTPStatus.FOUND

        real_lobby.cookies.delete(SESSION_COOKIE)
        signed_in_again = real_lobby.post(
            "/login",
            data={"username": _PERSON, "password": _TYPED_WORDS},
        )
        assert signed_in_again.status_code == HTTPStatus.SEE_OTHER
        assert real_lobby.get("/").is_success

    assert _TYPED_WORDS not in caplog.text


@pytest.mark.usefixtures("environment")
def test_the_real_stack_refuses_a_login_nobody_has_an_account_for() -> None:
    real_lobby = a_real_lobby()

    refused = real_lobby.post(
        "/login",
        data={"username": "nobody", "password": _TYPED_WORDS},
    )

    assert refused.status_code == HTTPStatus.OK
    assert SESSION_COOKIE not in real_lobby.cookies


@pytest.mark.usefixtures("bare_environment")
def test_an_instance_without_a_key_refuses_to_start() -> None:
    with pytest.raises(ConfigurationError, match="secret_key"):
        load_settings()


def test_an_instance_key_shorter_than_thirty_two_bytes_refuses_to_start(
    bare_environment: pytest.MonkeyPatch,
) -> None:
    too_short = "the words only this test types"
    bare_environment.setenv("PRESENTATOR_SECRET_KEY", too_short)

    with pytest.raises(ConfigurationError, match="secret_key") as refused:
        load_settings()

    assert too_short not in str(refused.value)


@pytest.mark.usefixtures("environment")
def test_the_instance_key_is_never_shown() -> None:
    settings = load_settings()

    assert _INSTANCE_KEY not in repr(settings)
    assert settings.secret_key.get_secret_value() == _INSTANCE_KEY


def a_signed_in_instance() -> TestClient:
    """The real stack with its admin created and its session open."""
    return signed_in(a_real_instance())


def logged_in(instance: Instance) -> TestClient:
    """A browser at that stack, signed in as the account it already carries."""
    client = TestClient(instance.lobby, follow_redirects=False)
    client.post("/login", data={"username": _PERSON, "password": _TYPED_WORDS})
    return client


def a_polled_source(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    *,
    armed: bool = True,
) -> Instance:
    """A real stack reading that remote, with the hook open while a secret arms it."""
    environment.setenv("PRESENTATOR_SOURCE_URL", remote.url)
    environment.setenv("PRESENTATOR_SOURCE_NAME", _SOURCE_NAME)
    if armed:
        environment.setenv(
            "PRESENTATOR_SOURCE_HOOK_SECRET",
            _WHAT_THE_HOST_CARRIES,
        )
    return a_real_instance()


def listed_addresses(page: str) -> list[str]:
    """The deck each row of the list links to, in the order they stand."""
    return re.findall(r'href="/deck/([^"]+)"', page)


def owner_of(database: Path, slug: str) -> str:
    """Who the stored deck belongs to, read straight from the real table."""
    kept = SqliteDeckStore(database=database).all()
    return next(deck.owner_id for deck in kept if deck.slug == slug)


def call_the_hook(lobby: TestClient, *, carrying: str) -> Response:
    return lobby.post(
        f"{HOOKS_PATH}/{_SOURCE_NAME}",
        headers={"authorization": f"Bearer {carrying}"},
    )


def test_a_deck_pushed_after_the_start_is_listed_after_one_tick_and_no_request(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    instance = a_polled_source(environment, remote)
    lobby = signed_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    before_the_tick = lobby.get("/").text
    asyncio.run(instance.poller.tick())
    after_the_tick = lobby.get("/").text

    assert EXAMPLE_TITLE not in before_the_tick
    assert EXAMPLE_TITLE in after_the_tick
    assert f'href="/deck/{EXAMPLE_SLUG}"' in after_the_tick


def test_the_hook_with_the_sources_secret_lists_a_push_at_once(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    lobby = signed_in(a_polled_source(environment, remote))
    remote.commit_example_deck(at=_PUSHED_AT)

    called = call_the_hook(lobby, carrying=_WHAT_THE_HOST_CARRIES)

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert EXAMPLE_TITLE in lobby.get("/").text


def test_a_hook_call_with_a_wrong_secret_leaves_the_list_as_it_was(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    lobby = signed_in(a_polled_source(environment, remote))
    remote.commit_example_deck(at=_PUSHED_AT)

    refused = call_the_hook(lobby, carrying="guessed")

    assert (refused.status_code, refused.content) == (HTTPStatus.NOT_FOUND, b"")
    assert EXAMPLE_TITLE not in lobby.get("/").text


def test_without_a_secret_the_real_stack_has_no_hook_address(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    lobby = signed_in(a_polled_source(environment, remote, armed=False))
    remote.commit_example_deck(at=_PUSHED_AT)
    lobby.cookies.clear()

    called = call_the_hook(lobby, carrying=_WHAT_THE_HOST_CARRIES)

    assert called.status_code == HTTPStatus.FOUND
    assert called.headers["location"] == "/login"
    assert EXAMPLE_TITLE not in signed_in(a_real_instance()).get("/").text


@pytest.mark.parametrize(
    "given",
    ["", "   ", "x", "a" * (SECRET_LENGTH - 1)],
    ids=["empty", "blank", "one character", "one character short"],
)
def test_a_hook_secret_that_guards_nothing_refuses_to_start(
    environment: pytest.MonkeyPatch,
    given: str,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_HOOK_SECRET", given)

    with pytest.raises(ConfigurationError, match="source_hook_secret"):
        load_settings()


def test_a_refused_hook_secret_is_never_quoted_back(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_HOOK_SECRET", _TYPED_WORDS)

    with pytest.raises(ConfigurationError) as refused:
        load_settings()

    assert _TYPED_WORDS not in str(refused.value)


def test_a_deck_the_real_stack_took_in_belongs_to_the_instance_admin(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(environment, remote)
    signed_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)
    database = tmp_path / "presentator.sqlite3"

    asyncio.run(instance.poller.tick())

    kept = SqliteDeckStore(database=database).all()
    admin = SqliteUserStore(database).first_admin()
    assert admin is not None
    assert [(deck.slug, deck.owner_id) for deck in kept] == [(EXAMPLE_SLUG, admin.id)]


def test_a_deck_the_real_stack_cannot_build_keeps_its_page_and_offers_no_view(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    environment.setenv("PRESENTATOR_TOOLCHAIN", str(tmp_path / "no-toolchain-here"))
    instance = a_polled_source(environment, remote)
    lobby = signed_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    asyncio.run(instance.poller.tick())
    page = lobby.get(f"/deck/{EXAMPLE_SLUG}")

    assert page.status_code == HTTPStatus.OK
    assert EXAMPLE_TITLE in page.text
    assert f"/deck/{EXAMPLE_SLUG}/presenter/" not in page.text


def test_a_tick_whose_fetch_exceeds_its_bound_leaves_the_list_answering(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_TIMEOUT_SECONDS", "0")
    instance = a_polled_source(environment, remote)
    lobby = signed_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    asyncio.run(instance.poller.tick())
    listed = lobby.get("/")

    assert listed.status_code == HTTPStatus.OK
    assert EXAMPLE_TITLE not in listed.text
    assert "<table" not in listed.text


def test_a_source_that_has_gone_away_still_leaves_its_decks_listed(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    instance = a_polled_source(environment, remote)
    lobby = signed_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)
    asyncio.run(instance.poller.tick())

    shutil.rmtree(remote.bare)
    listed = lobby.get("/")

    assert listed.status_code == HTTPStatus.OK
    assert EXAMPLE_TITLE in listed.text


@pytest.mark.usefixtures("environment")
def test_an_instance_without_a_source_shows_the_empty_list() -> None:
    listed = a_signed_in_instance().get("/")

    assert listed.status_code == HTTPStatus.OK
    assert "<table" not in listed.text


def test_a_deck_deleted_in_git_leaves_the_lobby_and_returns_as_the_same_deck(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(environment, remote)
    lobby = signed_in(instance)
    database = tmp_path / "presentator.sqlite3"
    remote.commit_example_deck(at=_PUSHED_AT)
    remote.commit_example_deck(at=_PUSHED_AT, into=_ANOTHER_SLUG)
    asyncio.run(instance.poller.tick())
    both_pushed = listed_addresses(lobby.get("/").text)
    owner_before_the_delete = owner_of(database, EXAMPLE_SLUG)

    remote.remove(EXAMPLE_SLUG, at=_DELETED_AT)
    asyncio.run(instance.poller.tick())
    after_the_delete = listed_addresses(lobby.get("/").text)

    remote.commit_example_deck(at=_PUSHED_AGAIN_AT)
    asyncio.run(instance.poller.tick())
    after_it_came_back = listed_addresses(lobby.get("/").text)

    assert sorted(both_pushed) == sorted([EXAMPLE_SLUG, _ANOTHER_SLUG])
    assert after_the_delete == [_ANOTHER_SLUG]
    assert after_it_came_back == [EXAMPLE_SLUG, _ANOTHER_SLUG]
    assert owner_of(database, EXAMPLE_SLUG) == owner_before_the_delete


def test_an_instance_carries_its_configured_source_from_the_moment_it_starts(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    signed_in(a_polled_source(environment, remote))

    started_again = a_polled_source(environment, remote)
    listed = logged_in(started_again).get("/")

    assert listed.status_code == HTTPStatus.OK
    assert remote.url in listed.text


def test_two_sources_each_list_their_own_decks_and_reconcile_alone(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    another_remote: GitRemote,
) -> None:
    first = a_polled_source(environment, remote)
    lobby = signed_in(first)
    remote.commit_example_deck(at=_PUSHED_AT)
    asyncio.run(first.poller.tick())
    another_remote.commit_example_deck(at=_PUSHED_AT, into=_ANOTHER_SLUG)
    environment.setenv("PRESENTATOR_SOURCE_URL", another_remote.url)
    environment.setenv("PRESENTATOR_SOURCE_NAME", _ANOTHER_SOURCE_NAME)
    both = a_real_instance()

    asyncio.run(both.poller.tick())
    listed_together = listed_addresses(lobby.get("/").text)

    remote.remove(EXAMPLE_SLUG, at=_DELETED_AT)
    asyncio.run(both.poller.tick())
    after_the_delete = listed_addresses(lobby.get("/").text)

    assert sorted(listed_together) == sorted([EXAMPLE_SLUG, _ANOTHER_SLUG])
    assert after_the_delete == [_ANOTHER_SLUG]
