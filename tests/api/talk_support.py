"""Both hosts of one instance over one database, as a browser reaches them."""

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from starlette.routing import Router
from starlette.types import ASGIApp, Message, Scope
from webauth.config import WebAuthConfig
from webauth.oidc import OidcRelyingParty
from webauth.oidc_login import AdmissionConfig

from presentator.adapters.identity import (
    ProcessWriteLock,
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    create_identity_tables,
)
from presentator.api.auth import InstalledAuth
from presentator.api.copresenter import CoPresenterSurface
from presentator.api.talk import create_talk, talk_content_security_policy
from presentator.application.identity import SESSION_LIFETIME
from presentator.contracts.models import Role, User
from presentator.host.main import Instance, served_by_host
from presentator.host.polling import Schedule
from tests.api.fake_issuer import FakeIssuer
from tests.api.lobby import (
    A_BROWSERS_ADDRESS,
    A_CLIENT_ID,
    A_TALK_ADDRESS,
    ADMIN,
    AN_ISSUER,
    NO_DECKS,
    GivenDecks,
    SigningIn,
    a_crowd,
    a_decks,
    a_lobby_app,
    a_relying_party,
    a_settings,
    a_talk_web_auth,
    an_installed_auth,
    hand_over_session_cookies,
    put_a_built_deck,
)
from tests.application.fakes import FakeDeckStore, FrozenClock
from tests.people import seed_user

ONLY_THIS_TEST_WROTE_IT: Final = "the secret beside the build directory"
A_DECK: Final = "a-deck"
B_DECK: Final = "b-deck"
NO_DECK: Final = "no-deck"
A: Final = User(id=ADMIN, username="felix", role=Role.ADMIN)
B: Final = User(id="id-of-anna", username="anna", role=Role.USER)
# A browser asking for a page, with no claim about where it came from.
A_BROWSER_ASKING_FOR_A_PAGE: Final = {"accept": "text/html"}
MEMBER: Final = ["/apps/presentator"]


def cookie_header(client: TestClient) -> str:
    """The `Cookie` header of everything this browser holds."""
    return "; ".join(f"{name}={value}" for name, value in client.cookies.items())


@dataclass(frozen=True, slots=True, kw_only=True)
class Hosts:
    """The lobby and the talk host behind the router, over one database."""

    router: Router
    lobby: FastAPI
    talk: FastAPI
    lobby_auth: InstalledAuth
    talk_auth: InstalledAuth
    lobby_config: WebAuthConfig
    talk_config: WebAuthConfig
    issuer: FakeIssuer
    database: Path
    clock: FrozenClock
    store: FakeDeckStore

    def browser(
        self, address: str, *, headers: dict[str, str] | None = None
    ) -> TestClient:
        """A browser with cookies of its own at one host, redirects not followed."""
        return TestClient(
            self.router,
            base_url=address,
            follow_redirects=False,
            headers=A_BROWSER_ASKING_FOR_A_PAGE if headers is None else headers,
        )

    def signed_in_at(
        self,
        address: str,
        person: User = A,
        *,
        headers: dict[str, str] | None = None,
    ) -> TestClient:
        """A browser holding a live session of this person at that host."""
        client = self.browser(address, headers=headers)
        hand_over_session_cookies(
            client,
            self.session_of(person),
            self.talk_config if address == A_TALK_ADDRESS else self.lobby_config,
        )
        return client

    def browser_with_the_cookies_of(
        self, signed_in: TestClient, address: str
    ) -> TestClient:
        """A fresh browser at another address, sent the cookies of a signed-in one."""
        return self.browser(
            address,
            headers={**A_BROWSER_ASKING_FOR_A_PAGE, "cookie": cookie_header(signed_in)},
        )

    def sign_in(
        self,
        client: TestClient,
        address: str,
        *,
        subject: str = "subject-1",
        groups: list[str] | None = None,
        next_path: str | None = None,
    ) -> Response:
        """A person signs in at the issuer through this host and the browser returns."""
        started = client.get(
            f"{address}/login/oidc",
            params={} if next_path is None else {"next": next_path},
        )
        query = self.issuer.authorize(
            started.headers["location"],
            subject=subject,
            groups=MEMBER if groups is None else groups,
            username=A.username,
        )
        return client.get(f"{address}/auth/callback?{query}")

    def give_a_deck_to(self, username: str) -> None:
        """Hand `a-deck` to the person the account store knows by this name."""
        with closing(sqlite3.connect(self.database)) as connection:
            (person,) = connection.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
        put_a_built_deck(self.store, slug=A_DECK, owner_id=person)

    def session_count(self) -> int:
        """How many session rows the one table holds."""
        with closing(sqlite3.connect(self.database)) as connection:
            (count,) = connection.execute("SELECT count(*) FROM sessions").fetchone()
        return int(count)

    def session_of(self, person: User) -> str:
        """The id of a new live session row of this person."""
        seed_user(self.database, person)
        now = self.clock.now()
        return (
            SqliteSessionRecordStore(self.database)
            .create(
                person.id,
                now=now,
                expires_at=now + SESSION_LIFETIME,
                refresh_token=None,
            )
            .id
        )


type SurfaceFactory = Callable[
    [FakeDeckStore, SqliteSessionRecordStore, InstalledAuth], CoPresenterSurface
]


def _relying_party_at(
    issuer: FakeIssuer, clock: FrozenClock, origin: str
) -> OidcRelyingParty:
    return a_relying_party(
        clock,
        post_token_request=issuer.post_token_request,
        fetch_key_set=issuer.fetch_key_set,
        origin=origin,
    )


def _both_auths(
    database: Path,
    issuer: FakeIssuer,
    clock: FrozenClock,
    admission: AdmissionConfig | None,
) -> tuple[InstalledAuth, InstalledAuth]:
    """The lobby's and the talk host's sign-in wiring over one issuer."""
    lobby_auth = an_installed_auth(
        clock,
        relying_party=_relying_party_at(issuer, clock, A_BROWSERS_ADDRESS),
        accounts=SqliteSubjectUserStore(database, ProcessWriteLock()),
    )
    if admission is not None:
        lobby_auth = replace(lobby_auth, admission=admission)
    talk_auth = replace(
        lobby_auth,
        config=a_talk_web_auth(clock),
        relying_party=_relying_party_at(issuer, clock, A_TALK_ADDRESS),
        content_security_policy=talk_content_security_policy(),
    )
    return lobby_auth, talk_auth


def two_hosts(
    tmp_path: Path,
    *,
    admission: AdmissionConfig | None = None,
    given: GivenDecks | None = None,
    copresenter: SurfaceFactory | None = None,
) -> Hosts:
    """Both hosts over real identity tables, a fake issuer and one set of decks.

    Without `given`, `a-deck` is A's and `b-deck` is B's; `no-deck` belongs to
    nobody. A given deck side is used instead, its store included. The
    co-presenter surface is made over this instance's own store, sessions and
    talk-host sign-in.
    """
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    people = a_crowd()
    clock = people.clock
    decks_store = (
        FakeDeckStore() if given is None or given.store is None else given.store
    )
    if given is None:
        put_a_built_deck(decks_store, slug=A_DECK, owner_id=A.id)
        put_a_built_deck(decks_store, slug=B_DECK, owner_id=B.id)
    decks = a_decks(
        replace(NO_DECKS if given is None else given, store=decks_store), clock
    )
    sessions = SqliteSessionRecordStore(database)
    issuer = FakeIssuer(issuer=AN_ISSUER, client_id=A_CLIENT_ID, now=clock.now())
    lobby_auth, talk_auth = _both_auths(database, issuer, clock, admission)
    talk_config = talk_auth.config
    lobby, _ = a_lobby_app(
        people=people,
        signing_in=SigningIn(sessions=sessions, auth=lobby_auth, decks=decks),
    )
    talk = create_talk(
        sessions=sessions,
        decks=decks,
        auth=talk_auth,
        lobby_origin=A_BROWSERS_ADDRESS,
        copresenter=(
            None
            if copresenter is None
            else copresenter(decks_store, sessions, talk_auth)
        ),
    )
    never = Schedule(chore=lambda: None, interval=timedelta(days=1))
    router = served_by_host(
        Instance(lobby=lobby, talk=talk, poller=never, session_sweep=never),
        a_settings(),
    )
    return Hosts(
        router=router,
        lobby=lobby,
        talk=talk,
        lobby_auth=lobby_auth,
        talk_auth=talk_auth,
        lobby_config=lobby_auth.config,
        talk_config=talk_config,
        issuer=issuer,
        database=database,
        clock=clock,
        store=decks_store,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Answer:
    """What an app answered at the ASGI door: the status and the headers."""

    status: int
    headers: dict[str, str]


async def _answer(app: ASGIApp, scope: Scope) -> Answer:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    return Answer(
        status=start["status"],
        headers={name.decode(): value.decode() for name, value in start["headers"]},
    )


def ask_at_the_door(
    app: ASGIApp,
    *,
    hosts: tuple[str, ...],
    path: str,
    cookie: str | None = None,
) -> Answer:
    """Send a GET into an app with exactly these `Host` headers, none or several.

    The client libraries always send one; a scope with none or two is what a
    parser that let it through would hand an app.
    """
    headers = [(b"host", host.encode()) for host in hosts]
    headers.append((b"accept", b"text/html"))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "root_path": "",
    }
    return asyncio.run(_answer(app, scope))


def cookies_without_values(response: Response) -> list[str]:
    """Every `Set-Cookie` entry, its name and attributes kept, its random value not."""
    return [
        f"{name}=<value>{attributes}"
        for name, attributes in (
            (entry.split("=", 1)[0], ";" + entry.partition(";")[2])
            for entry in response.headers.get_list("set-cookie")
        )
    ]
