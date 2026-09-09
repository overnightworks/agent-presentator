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

from presentator.adapters.decks import (
    SqliteDeckStore,
    SqliteSourceStore,
)
from presentator.adapters.identity import (
    SqliteUserStore,
    TokenIdentifierFactory,
)
from presentator.adapters.secrets import secret_box
from presentator.adapters.sqlite import rows
from presentator.api.auth import SESSION_COOKIE
from presentator.api.hooks import hook_address
from presentator.application.decks import hash_webhook_secret
from presentator.application.identity import FAILURES_BEFORE_THROTTLE
from presentator.contracts.decks import SourceWrite
from presentator.host import main
from presentator.host.config import (
    NO_BOUND_AT_ALL,
    ConfigurationError,
    load_settings,
)
from presentator.host.main import Instance
from tests.api.lobby import A_BROWSERS_HEADERS
from tests.conftest import (
    EXAMPLE_SLUG,
    EXAMPLE_TITLE,
    GitRemote,
    with_the_program,
)

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
_WHAT_THE_GIT_HOST_EXPECTS = "the read-only words only this test made up"
_CREDENTIAL_VARIABLE = "A_READ_ONLY_TOKEN"
_ANOTHER_INSTANCE_KEY = "the key another instance carries"
_A_HALF_NAMED_SANDBOX = "named-here-but-not-beside-it"
_THE_BUILD_IMAGE = "the-build-image-of-this-deployment"
_THE_BUILDS_VOLUME = "the-builds-volume-of-this-deployment"
_THE_VERSION_A_SUBPATH_NEEDS = "1.45"
_A_DRIVER = "overlay2"
# A `docker` this test wrote: one daemon that carries the image and bounds a
# container's own filesystem but builds no deck, one too old to keep a build
# inside its own directory, and one that will not run a container under a size.
_A_DAEMON_THAT_BUILDS_NOTHING = f"""#!/bin/sh
case "$1" in
    version) echo "1.52";;
    info) echo "{_A_DRIVER}";;
    image) echo "sha256:the-image";;
    create) exit 0;;
    start) exit 0;;
    rm) exit 0;;
    ps) ;;
    run) echo "there is no such deck" >&2; exit 1;;
esac
"""
_A_DAEMON_TOO_OLD_TO_BUILD_ON = f"""#!/bin/sh
case "$1" in
    version) echo "1.44";;
    info) echo "{_A_DRIVER}";;
esac
"""
_A_DAEMON_THAT_TAKES_NO_SIZE = f"""#!/bin/sh
case "$1" in
    version) echo "1.52";;
    info) echo "{_A_DRIVER}";;
    image) echo "sha256:the-image";;
    create) echo "this driver takes no size" >&2; exit 125;;
    rm) exit 0;;
esac
"""


@pytest.fixture
def bare_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRESENTATOR_SECRET_KEY", raising=False)
    monkeypatch.setenv("PRESENTATOR_DATABASE", str(tmp_path / "presentator.sqlite3"))
    # `remote`/`another_remote` (tests/conftest.py) write their bare
    # repositories straight into `tmp_path`, the one directory every real
    # git address these tests build stands under; naming it as the mount
    # lets a `file://` fixture keep standing in for "any git remote" without
    # every such test naming the mount for itself.
    monkeypatch.setenv("PRESENTATOR_LOCAL_SOURCES_MOUNT", str(tmp_path))
    return monkeypatch


@pytest.fixture
def environment(bare_environment: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    bare_environment.setenv("PRESENTATOR_SECRET_KEY", _INSTANCE_KEY)
    # These tests are about the lobby this root composes, not about where a
    # deck's code runs; the development run is what asks nothing of a daemon,
    # and the tests below that are about the sandbox say so themselves.
    bare_environment.setenv("PRESENTATOR_BUILD_RUNNER", "host")
    return bare_environment


def a_real_instance() -> Instance:
    """The whole stack the composition root builds, on the configured database."""
    return main.build_instance(load_settings())


def a_real_lobby() -> TestClient:
    """A browser at that stack."""
    return TestClient(
        a_real_instance().lobby, follow_redirects=False, headers=A_BROWSERS_HEADERS
    )


def signed_in(instance: Instance) -> TestClient:
    """A browser at that stack with the instance's admin created and signed in."""
    client = TestClient(
        instance.lobby, follow_redirects=False, headers=A_BROWSERS_HEADERS
    )
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


def a_browser(instance: Instance, *, peer: str = _PEER) -> TestClient:
    """A browser whose ASGI peer is an address, so a proxy list can trust it."""
    return TestClient(
        instance.lobby,
        follow_redirects=False,
        client=(peer, 50000),
        headers=A_BROWSERS_HEADERS,
    )


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


@pytest.mark.parametrize(
    "half",
    [None, "PRESENTATOR_BUILD_IMAGE", "PRESENTATOR_BUILD_VOLUME"],
    ids=["neither of them", "an image without a volume", "a volume without an image"],
)
def test_an_instance_that_does_not_say_what_builds_a_deck_refuses_to_start(
    environment: pytest.MonkeyPatch,
    half: str | None,
) -> None:
    # Nothing said about where a build runs is a build in a container, and a
    # container this environment does not name is no instance at all.
    environment.delenv("PRESENTATOR_BUILD_RUNNER")
    if half is not None:
        environment.setenv(half, _A_HALF_NAMED_SANDBOX)

    with pytest.raises(ConfigurationError, match="build_image and build_volume"):
        main.build_instance(load_settings())


def test_an_instance_whose_daemon_cannot_hold_a_build_in_its_place_refuses(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    a_container_run(environment, tmp_path, _A_DAEMON_TOO_OLD_TO_BUILD_ON)

    with pytest.raises(ConfigurationError, match=_THE_VERSION_A_SUBPATH_NEEDS):
        main.build_instance(load_settings())


def test_an_instance_whose_machine_cannot_bound_a_build_refuses_to_start(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    a_container_run(environment, tmp_path, _A_DAEMON_THAT_TAKES_NO_SIZE)

    # Nothing bounding what a build writes beside its talk is not something an
    # instance may find out about after it has served a deck.
    with pytest.raises(ConfigurationError, match=_A_DRIVER):
        main.build_instance(load_settings())


def test_an_instance_told_in_as_many_words_to_do_without_it_starts_and_says_so(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a_container_run(environment, tmp_path, _A_DAEMON_THAT_TAKES_NO_SIZE)
    environment.setenv("PRESENTATOR_BUILD_DISK", NO_BOUND_AT_ALL)

    main.build_instance(load_settings())

    assert "build_disk" in caplog.text


def test_an_instance_whose_disk_bound_came_out_blank_refuses_to_start(
    environment: pytest.MonkeyPatch,
) -> None:
    # A value nobody watched interpolate is not a decision to build without
    # the one bound on what a deck writes beside its talk.
    environment.setenv("PRESENTATOR_BUILD_DISK", "  ")

    with pytest.raises(ConfigurationError, match="build_disk"):
        load_settings()


def test_a_deck_an_instance_can_only_build_in_a_container_it_lacks_offers_no_view(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    a_container_run(environment, tmp_path, _A_DAEMON_THAT_BUILDS_NOTHING)
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    asyncio.run(instance.poller.tick())
    page = lobby.get(f"/deck/{EXAMPLE_SLUG}")

    assert page.status_code == HTTPStatus.OK
    assert EXAMPLE_TITLE in page.text
    assert f"/deck/{EXAMPLE_SLUG}/presenter/" not in page.text


def a_container_run(
    environment: pytest.MonkeyPatch,
    tmp_path: Path,
    docker: str,
) -> None:
    """An instance that builds every deck in a container, on that daemon."""
    environment.setenv("PRESENTATOR_BUILD_RUNNER", "container")
    environment.setenv("PRESENTATOR_BUILD_IMAGE", _THE_BUILD_IMAGE)
    environment.setenv("PRESENTATOR_BUILD_VOLUME", _THE_BUILDS_VOLUME)
    with_the_program(environment, tmp_path, "docker", docker)


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
    client = TestClient(
        instance.lobby, follow_redirects=False, headers=A_BROWSERS_HEADERS
    )
    client.post("/login", data={"username": _PERSON, "password": _TYPED_WORDS})
    return client


def a_polled_source(
    remote: GitRemote,
    tmp_path: Path,
    *,
    armed: bool = True,
    name: str = _SOURCE_NAME,
) -> Instance:
    """A real stack with that remote stored as a source after first start."""
    instance = a_real_instance()
    signed_in(instance)
    database = tmp_path / "presentator.sqlite3"
    admin = SqliteUserStore(database).first_admin()
    assert admin is not None
    added = sources_over(database).add(
        SourceWrite(
            name=name,
            url=remote.url,
            ref="main",
            owner_id=admin.id,
            access_secret=_WHAT_THE_GIT_HOST_EXPECTS,
            hook_secret_hash=hash_webhook_secret(_WHAT_THE_HOST_CARRIES),
        ),
    )
    assert added is not None
    if not armed:
        with rows(database) as cursor:
            cursor.execute(
                "UPDATE sources SET hook_secret_hash = NULL WHERE name = ?",
                (name,),
            )
    return instance


def listed_addresses(page: str) -> list[str]:
    """The deck each row of the list links to, in the order they stand."""
    return re.findall(r'href="/deck/([^"]+)"', page)


def owner_of(database: Path, slug: str) -> str:
    """Who the stored deck belongs to, read straight from the real table."""
    kept = SqliteDeckStore(database=database).all()
    return next(deck.owner_id for deck in kept if deck.slug == slug)


def call_the_hook(lobby: TestClient, *, carrying: str) -> Response:
    return lobby.post(
        hook_address(_SOURCE_NAME),
        headers={"authorization": f"Bearer {carrying}"},
    )


def sources_over(
    database: Path, *, instance_key: str = _INSTANCE_KEY
) -> SqliteSourceStore:
    """The real source store over the instance's file, keyed as it says.

    What the instance itself writes is keyed by its own instance key; another
    key stands for a row this installation did not write.
    """
    return SqliteSourceStore(
        database=database,
        identifiers=TokenIdentifierFactory(),
        box=secret_box(instance_key),
    )


@pytest.mark.usefixtures("environment")
def test_the_real_stack_pulls_with_the_secret_its_own_key_can_open(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    asyncio.run(instance.poller.tick())
    database = tmp_path / "presentator.sqlite3"
    stored = sources_over(database).all()[0]
    remote.commit_example_deck(at=_PUSHED_AT)

    sources_over(database, instance_key=_ANOTHER_INSTANCE_KEY).put_credential(
        stored.id,
        _WHAT_THE_GIT_HOST_EXPECTS,
    )
    asyncio.run(instance.poller.tick())
    while_the_row_held_what_this_key_cannot_open = lobby.get("/").text

    sources_over(database).put_credential(stored.id, _WHAT_THE_GIT_HOST_EXPECTS)
    asyncio.run(instance.poller.tick())
    after_this_instance_wrote_it = lobby.get("/").text

    assert EXAMPLE_TITLE not in while_the_row_held_what_this_key_cannot_open
    assert EXAMPLE_TITLE in after_this_instance_wrote_it


@pytest.mark.usefixtures("environment")
def test_a_deck_pushed_after_the_start_is_listed_after_one_tick_and_no_request(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    before_the_tick = lobby.get("/").text
    asyncio.run(instance.poller.tick())
    after_the_tick = lobby.get("/").text

    assert EXAMPLE_TITLE not in before_the_tick
    assert EXAMPLE_TITLE in after_the_tick
    assert f'href="/deck/{EXAMPLE_SLUG}"' in after_the_tick


@pytest.mark.usefixtures("environment")
def test_the_hook_with_the_sources_secret_lists_a_push_at_once(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    asyncio.run(instance.poller.tick())
    remote.commit_example_deck(at=_PUSHED_AT)

    called = call_the_hook(lobby, carrying=_WHAT_THE_HOST_CARRIES)

    assert called.status_code == HTTPStatus.NO_CONTENT
    assert EXAMPLE_TITLE in lobby.get("/").text


@pytest.mark.usefixtures("environment")
def test_a_hook_call_with_a_wrong_secret_leaves_the_list_as_it_was(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    asyncio.run(instance.poller.tick())
    remote.commit_example_deck(at=_PUSHED_AT)

    refused = call_the_hook(lobby, carrying="guessed")

    assert (refused.status_code, refused.content) == (HTTPStatus.NOT_FOUND, b"")
    assert EXAMPLE_TITLE not in lobby.get("/").text


@pytest.mark.usefixtures("environment")
def test_a_source_without_a_webhook_hash_is_refused_alike_not_sent_to_login(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    lobby = logged_in(a_polled_source(remote, tmp_path, armed=False))
    remote.commit_example_deck(at=_PUSHED_AT)
    lobby.cookies.clear()

    called = call_the_hook(lobby, carrying=_WHAT_THE_HOST_CARRIES)

    assert (called.status_code, called.content) == (HTTPStatus.NOT_FOUND, b"")
    assert called.headers.get("location") is None
    assert EXAMPLE_TITLE not in signed_in(a_real_instance()).get("/").text


@pytest.mark.usefixtures("environment")
def test_a_deck_the_real_stack_took_in_belongs_to_the_instance_admin(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
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
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    asyncio.run(instance.poller.tick())
    page = lobby.get(f"/deck/{EXAMPLE_SLUG}")

    assert page.status_code == HTTPStatus.OK
    assert EXAMPLE_TITLE in page.text
    assert f"/deck/{EXAMPLE_SLUG}/presenter/" not in page.text


def test_a_tick_whose_fetch_exceeds_its_bound_leaves_the_list_answering(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_TIMEOUT_SECONDS", "0")
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
    remote.commit_example_deck(at=_PUSHED_AT)

    asyncio.run(instance.poller.tick())
    listed = lobby.get("/")

    assert listed.status_code == HTTPStatus.OK
    assert EXAMPLE_TITLE not in listed.text
    assert "<table" not in listed.text


@pytest.mark.usefixtures("environment")
def test_a_source_that_has_gone_away_still_leaves_its_decks_listed(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
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


def test_a_configured_source_url_in_the_environment_changes_nothing(
    environment: pytest.MonkeyPatch,
    remote: GitRemote,
) -> None:
    environment.setenv("PRESENTATOR_SOURCE_URL", remote.url)
    environment.setenv("PRESENTATOR_SOURCE_NAME", _SOURCE_NAME)
    environment.setenv("PRESENTATOR_SOURCE_CREDENTIAL", _CREDENTIAL_VARIABLE)
    environment.setenv(_CREDENTIAL_VARIABLE, _WHAT_THE_GIT_HOST_EXPECTS)
    lobby = a_signed_in_instance()

    listed = lobby.get("/")
    sources = lobby.get("/settings/sources")

    assert listed.status_code == HTTPStatus.OK
    assert "<table" not in listed.text
    assert "<table" not in sources.text
    assert remote.url not in sources.text


@pytest.mark.usefixtures("environment")
def test_a_deck_deleted_in_git_leaves_the_lobby_and_returns_as_the_same_deck(
    remote: GitRemote,
    tmp_path: Path,
) -> None:
    instance = a_polled_source(remote, tmp_path)
    lobby = logged_in(instance)
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


@pytest.mark.usefixtures("environment")
def test_two_sources_each_list_their_own_decks_and_reconcile_alone(
    remote: GitRemote,
    another_remote: GitRemote,
    tmp_path: Path,
) -> None:
    first = a_polled_source(remote, tmp_path)
    lobby = logged_in(first)
    remote.commit_example_deck(at=_PUSHED_AT)
    asyncio.run(first.poller.tick())
    another_remote.commit_example_deck(at=_PUSHED_AT, into=_ANOTHER_SLUG)
    database = tmp_path / "presentator.sqlite3"
    admin = SqliteUserStore(database).first_admin()
    assert admin is not None
    added = sources_over(database).add(
        SourceWrite(
            name=_ANOTHER_SOURCE_NAME,
            url=another_remote.url,
            ref="main",
            owner_id=admin.id,
            access_secret=_WHAT_THE_GIT_HOST_EXPECTS,
            hook_secret_hash=hash_webhook_secret(_WHAT_THE_HOST_CARRIES),
        ),
    )
    assert added is not None
    both = a_real_instance()

    asyncio.run(both.poller.tick())
    listed_together = listed_addresses(lobby.get("/").text)

    remote.remove(EXAMPLE_SLUG, at=_DELETED_AT)
    asyncio.run(both.poller.tick())
    after_the_delete = listed_addresses(lobby.get("/").text)

    assert sorted(listed_together) == sorted([EXAMPLE_SLUG, _ANOTHER_SLUG])
    assert after_the_delete == [_ANOTHER_SLUG]
