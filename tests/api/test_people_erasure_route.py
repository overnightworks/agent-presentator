"""S9 (#293): `DELETE /api/admin/people/{subject}` takes hal's access token alone."""

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus

import pytest
import uvicorn
from fastapi.testclient import TestClient
from httpx2 import Response

from presentator.api.people import AdminPeople
from presentator.application.people import People
from presentator.host import main
from presentator.host.config import ConfigurationError, load_settings
from tests.api.fake_issuer import FakeIssuer
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_BROWSERS_HEADERS,
    A_CLIENT_ID,
    A_TALK_ADDRESS,
    AN_ISSUER,
    NOW,
    Lobby,
    SigningIn,
    a_browser_at,
    a_lobby_app,
    a_relying_party,
    a_talk_app,
    an_installed_auth,
)
from tests.application.fakes import (
    FakeDeckFolders,
    FakePeople,
    FrozenClock,
)
from tests.application.person_instance_support import (
    ANOTHER,
    THE_PERSON,
    Instance,
    an_instance,
)

HAL = "hal-client"
A_SECOND_CLIENT = "hal-second-client"
THE_CALLER = "keycloak-subject-of-the-operator"
ADMINS = ["/apps/presentator", "/apps/presentator/admin"]
ADMINS_OF_A_SUBGROUP = ["/apps/presentator", "/apps/presentator/admin/ops"]
MEMBERS = ["/apps/presentator"]
_PATH = "/api/admin/people/"
_NO_STORE = "no-store"


@dataclass(frozen=True, slots=True)
class Scene:
    """A lobby over two people with every kind of row, and the issuer of its tokens."""

    instance: Instance
    issuer: FakeIssuer
    people: FakePeople
    lobby: Lobby

    def token(
        self,
        *,
        groups: list[str] = ADMINS,
        authorized_party: str = HAL,
        audience: str | None = None,
    ) -> dict[str, str]:
        minted = self.issuer.mint_access_token(
            subject=THE_CALLER,
            groups=groups,
            authorized_party=authorized_party,
            audience=audience,
        )
        return {"authorization": f"Bearer {minted}"}

    def erase(
        self,
        subject: str = THE_PERSON.id,
        *,
        headers: dict[str, str] | None = None,
        query: str = "",
    ) -> Response:
        return self.lobby.client.delete(
            f"{_PATH}{subject}{query}",
            headers=self.token() if headers is None else headers,
        )

    def state(self) -> tuple[object, ...]:
        return self.instance.state()


def a_scene(
    *,
    clients: frozenset[str] | None = frozenset({HAL}),
    folders: FakeDeckFolders | None = None,
    lock_wait: timedelta = timedelta(milliseconds=50),
) -> Scene:
    instance = an_instance(folders=folders)
    clock = FrozenClock(instant=NOW)
    people = FakePeople(users=instance.users, sessions=instance.sessions, clock=clock)
    issuer = FakeIssuer(issuer=AN_ISSUER, client_id=A_CLIENT_ID, now=NOW)
    auth = an_installed_auth(
        clock,
        relying_party=a_relying_party(clock, fetch_key_set=issuer.fetch_key_set),
    )
    app, _ = a_lobby_app(
        people=people,
        signing_in=SigningIn(
            sessions=instance.sessions, auth=auth, decks=instance.decks
        ),
        admin_people=None
        if clients is None
        else AdminPeople(
            people=People(
                decks=instance.decks,
                accounts=instance.users,
                sessions=instance.sessions,
                preferences=instance.preferences,
                key_drafts=instance.fakes.key_drafts,
                lock_wait=lock_wait,
            ),
            admitted_clients=clients,
        ),
    )
    return Scene(
        instance=instance,
        issuer=issuer,
        people=people,
        lobby=a_browser_at(
            app, people=people, base_url=A_BROWSERS_ADDRESS, headers=A_BROWSERS_HEADERS
        ),
    )


def _no_authorization() -> dict[str, str]:
    return {}


@pytest.mark.parametrize(
    ("cell", "status"),
    [
        ("no token", HTTPStatus.UNAUTHORIZED),
        ("another audience", HTTPStatus.UNAUTHORIZED),
        ("azp not admitted", HTTPStatus.UNAUTHORIZED),
        ("without the admin group", HTTPStatus.FORBIDDEN),
    ],
)
def test_a_token_the_configuration_does_not_admit_deletes_nothing(
    cell: str, status: HTTPStatus
) -> None:
    """E5, S9: 401 or 403 by the cell, and the person stays."""
    scene = a_scene()
    before = scene.state()
    headers = {
        "no token": _no_authorization(),
        "another audience": scene.token(audience="presentator-talk-test"),
        "azp not admitted": scene.token(authorized_party="another-client"),
        "without the admin group": scene.token(groups=MEMBERS),
    }[cell]

    answer = scene.erase(headers=headers)

    assert answer.status_code == status
    assert answer.headers["cache-control"] == _NO_STORE
    assert scene.state() == before


@pytest.mark.parametrize(
    ("clients", "authorized_party", "groups"),
    [
        (frozenset({HAL}), HAL, ADMINS),
        (frozenset({HAL, A_SECOND_CLIENT}), A_SECOND_CLIENT, ADMINS),
        (frozenset({HAL}), HAL, ADMINS_OF_A_SUBGROUP),
    ],
)
def test_a_token_the_configuration_admits_erases_the_person(
    clients: frozenset[str], authorized_party: str, groups: list[str]
) -> None:
    """E5, S9: an admitted client, a second one, or an admin subgroup erases."""
    scene = a_scene(clients=clients)

    answer = scene.erase(
        headers=scene.token(authorized_party=authorized_party, groups=groups)
    )

    assert answer.status_code == HTTPStatus.OK
    assert THE_PERSON.id not in scene.instance.users.users


def test_an_admins_session_cookie_alone_gets_401_and_deletes_nothing() -> None:
    """S9 (webauth#350 rule 2): a cookie counts for nothing on these routes."""
    scene = a_scene()
    scene.lobby.sign_in()
    before = scene.state()

    answer = scene.erase(headers={})

    assert answer.status_code == HTTPStatus.UNAUTHORIZED
    assert scene.state() == before


def test_a_real_run_answers_the_envelope_with_what_it_removed() -> None:
    """E5, edge people.md: one body, `dry_run` false, the counts what was removed."""
    scene = a_scene()
    expected_kinds = scene.erase(query="?dry_run=true").json()["kinds"]

    answer = scene.erase(query="?dry_run=false")

    assert answer.status_code == HTTPStatus.OK
    assert answer.json() == {
        "people_erasure": 1,
        "app": "presentator",
        "subject": THE_PERSON.id,
        "dry_run": False,
        "kinds": expected_kinds,
        "affects_others": [],
    }
    assert THE_PERSON.id not in scene.instance.users.users
    assert ANOTHER.id in scene.instance.users.users


def test_a_dry_run_answers_the_same_body_and_changes_nothing() -> None:
    """D12, edge people.md: the preview is the real run's body with `dry_run` true."""
    scene = a_scene()
    before = scene.state()

    preview = scene.erase(query="?dry_run=true")

    assert scene.state() == before
    assert preview.json()["dry_run"] is True
    assert preview.json()["kinds"]
    real = scene.erase()
    assert real.json() == {**preview.json(), "dry_run": False}


def test_an_unparsable_dry_run_answers_422_and_deletes_nothing() -> None:
    """D12: a flag that is not a boolean never turns into a real run."""
    scene = a_scene()
    before = scene.state()

    answer = scene.erase(query="?dry_run=maybe")

    assert answer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert scene.state() == before


def test_an_unknown_subject_answers_zero_and_creates_no_row() -> None:
    """S9: 200 with zero counts, and no `users` row for target or caller."""
    scene = a_scene()
    before = scene.state()

    answer = scene.erase("nobody-at-all")

    assert answer.status_code == HTTPStatus.OK
    kinds = answer.json()["kinds"]
    assert kinds
    assert {one["count"] for one in kinds} == {0}
    assert scene.state() == before
    assert set(scene.instance.users.users) == {THE_PERSON.id, ANOTHER.id}


def test_a_repeat_run_finds_nothing_left() -> None:
    """S9, edge people.md: idempotent, the second run is at zero."""
    scene = a_scene()
    scene.erase()

    repeat = scene.erase()

    assert repeat.status_code == HTTPStatus.OK
    kinds = repeat.json()["kinds"]
    assert kinds
    assert {one["count"] for one in kinds} == {0}


def test_a_running_build_answers_409_with_the_wait_and_deletes_nothing() -> None:
    """S9, D8: busy is 409, `Retry-After` the whole seconds of the wait, no deletion."""
    scene = a_scene(lock_wait=timedelta(milliseconds=50))
    before = scene.state()
    with scene.instance.decks.one_at_a_time:
        answer = scene.erase()

    assert answer.status_code == HTTPStatus.CONFLICT
    assert answer.headers["retry-after"] == "1"
    assert answer.headers["cache-control"] == _NO_STORE
    assert answer.json() == {
        "people_erasure": 1,
        "app": "presentator",
        "subject": THE_PERSON.id,
        "busy": [{"kind": "running-work", "count": 1}],
    }
    assert scene.state() == before


def test_a_mirror_that_cannot_go_answers_500_and_a_repeat_finishes() -> None:
    """D9: a disk leftover answers 500 without a source name, and the person stays."""
    stuck = FakeDeckFolders(stuck=frozenset({"talks-id"}))
    scene = a_scene(folders=stuck)

    answer = scene.erase()

    assert answer.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert answer.json() == {
        "people_erasure": 1,
        "app": "presentator",
        "subject": THE_PERSON.id,
        "leftover": [{"kind": "sources", "count": 1}],
    }
    assert answer.headers["cache-control"] == _NO_STORE
    assert THE_PERSON.id in scene.instance.users.users
    stuck.stuck = frozenset()
    assert scene.erase().status_code == HTTPStatus.OK


def test_an_empty_setting_serves_no_such_route() -> None:
    """D6: with no client named the prefix falls to the session gate like any path."""
    scene = a_scene(clients=None)
    before = scene.state()

    answer = scene.erase()

    elsewhere = scene.lobby.client.delete("/api/nothing/here", headers=scene.token())
    assert answer.status_code == elsewhere.status_code
    assert answer.status_code != HTTPStatus.OK
    assert scene.state() == before


def test_the_talk_host_serves_no_such_route() -> None:
    """S9 (#293), matrix host axis: the talk host serves no such route."""
    scene = a_scene()
    before = scene.state()
    talk = TestClient(
        a_talk_app(people=scene.people),
        base_url=A_TALK_ADDRESS,
        follow_redirects=False,
    )

    answer = talk.delete(f"{_PATH}{THE_PERSON.id}", headers=scene.token())

    assert answer.status_code != HTTPStatus.OK
    assert scene.state() == before


@pytest.mark.parametrize("query", ["?dry_run=true", "", "?dry_run=maybe"])
def test_every_answer_carries_no_store(query: str) -> None:
    """S9, edge people.md: no answer of these routes may be cached."""
    scene = a_scene()

    answer = scene.erase(query=query)

    assert answer.headers["cache-control"] == _NO_STORE


@pytest.mark.parametrize(
    ("query", "subject", "build_running"),
    [
        ("?dry_run=true", THE_PERSON.id, False),
        ("", THE_PERSON.id, False),
        ("", "nobody-at-all", False),
        ("", THE_PERSON.id, True),
    ],
)
def test_one_call_writes_one_audit_line_naming_nobody(
    query: str,
    subject: str,
    *,
    build_running: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D10: one INFO line on the route's logger; neither subject in any record."""
    scene = a_scene(lock_wait=timedelta(milliseconds=50))
    caplog.set_level(logging.DEBUG)

    with scene.instance.decks.one_at_a_time if build_running else nullcontext():
        scene.erase(subject, query=query)

    ours = [
        record for record in caplog.records if record.name == "presentator.api.people"
    ]
    assert [record.levelno for record in ours] == [logging.INFO]
    # The test client logs its own request line; only the server's records count.
    everything = " ".join(
        f"{record.getMessage()} {record.args}"
        for record in caplog.records
        if not record.name.startswith("httpx2")
    )
    assert THE_CALLER not in everything
    assert subject not in everything


def test_the_server_logs_no_request_path(environment: pytest.MonkeyPatch) -> None:
    """Q4 (#293): a path under the admin prefix carries a subject; no log line may."""
    configs: list[uvicorn.Config] = []

    class ServerThatOnlyRecordsItsConfig:
        def __init__(self, config: uvicorn.Config) -> None:
            configs.append(config)

        async def serve(self) -> None:
            return None

    server_log = logging.getLogger("uvicorn.error")
    for attribute in ("level", "propagate"):
        environment.setattr(server_log, attribute, getattr(server_log, attribute))
    environment.setattr(main.uvicorn, "Server", ServerThatOnlyRecordsItsConfig)
    main.main()
    [config] = configs
    emitted: list[str] = []

    class Emitted(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record.getMessage())

    config.configure_logging()
    environment.setattr(server_log, "handlers", [Emitted()])
    server_log.info('127.0.0.1 - "WebSocket /api/admin/people/subject-id" 403')
    server_log.error("the server failed")

    assert emitted == ["the server failed"]


@pytest.mark.parametrize(
    ("given", "admitted"),
    [
        ("", frozenset[str]()),
        ("hal", frozenset({"hal"})),
        ("hal, second-hal", frozenset({"hal", "second-hal"})),
    ],
)
def test_the_admitted_token_clients_are_the_comma_separated_ids(
    environment: pytest.MonkeyPatch, given: str, admitted: frozenset[str]
) -> None:
    """D6: the setting keeps the environment's comma format."""
    environment.setenv("PRESENTATOR_ADMIN_TOKEN_CLIENTS", given)

    assert load_settings().admitted_token_clients() == admitted


@pytest.mark.parametrize("given", [" ", "hal,", ",hal", "hal,,second-hal"])
def test_an_empty_entry_among_the_admitted_token_clients_refuses_to_start(
    environment: pytest.MonkeyPatch, given: str
) -> None:
    """D6: a list with an empty entry is a typo, not a decision."""
    environment.setenv("PRESENTATOR_ADMIN_TOKEN_CLIENTS", given)

    with pytest.raises(ConfigurationError, match="admin_token_clients"):
        load_settings()


def test_without_a_named_client_the_admin_prefix_falls_to_the_session_gate(
    environment: pytest.MonkeyPatch,
) -> None:
    """D6: the prefix answers like any unknown path until a client is named."""
    path = "/api/admin/people/someone"
    browser = TestClient(
        main.build_instance(load_settings()).lobby,
        base_url=A_BROWSERS_ADDRESS,
        follow_redirects=False,
    )
    without = browser.delete(path)
    elsewhere = browser.delete("/api/other/people/someone")
    environment.setenv("PRESENTATOR_ADMIN_TOKEN_CLIENTS", "hal")
    gated = TestClient(
        main.build_instance(load_settings()).lobby,
        base_url=A_BROWSERS_ADDRESS,
        follow_redirects=False,
    ).delete(path)

    assert without.status_code == elsewhere.status_code
    assert "www-authenticate" not in without.headers
    assert gated.status_code == HTTPStatus.UNAUTHORIZED
    assert gated.headers["www-authenticate"].startswith("Bearer")
