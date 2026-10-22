"""Both hosts of one instance over one database, as a browser reaches them."""

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Router
from starlette.types import ASGIApp, Message, Scope
from webauth.config import WebAuthConfig

from presentator.adapters.identity import (
    SqliteSessionRecordStore,
    SqliteSubjectUserStore,
    create_identity_tables,
)
from presentator.api.auth import InstalledAuth
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

A_DECK: Final = "a-deck"
B_DECK: Final = "b-deck"
NO_DECK: Final = "no-deck"
A: Final = User(id=ADMIN, username="felix", role=Role.ADMIN)
B: Final = User(id="id-of-anna", username="anna", role=Role.USER)
# A browser asking for a page, with no claim about where it came from.
A_BROWSER_ASKING_FOR_A_PAGE: Final = {"accept": "text/html"}


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


def two_hosts(tmp_path: Path, *, store: FakeDeckStore | None = None) -> Hosts:
    """Both hosts over real identity tables, a fake issuer and A's and B's decks.

    `a-deck` is A's and `b-deck` is B's; `no-deck` belongs to nobody.
    """
    database = tmp_path / "identity.sqlite3"
    create_identity_tables(database)
    people = a_crowd()
    clock = people.clock
    decks_store = FakeDeckStore() if store is None else store
    put_a_built_deck(decks_store, slug=A_DECK, owner_id=A.id)
    put_a_built_deck(decks_store, slug=B_DECK, owner_id=B.id)
    decks = a_decks(replace(NO_DECKS, store=decks_store), clock)
    sessions = SqliteSessionRecordStore(database)
    issuer = FakeIssuer(issuer=AN_ISSUER, client_id=A_CLIENT_ID, now=clock.now())
    lobby_auth = an_installed_auth(
        clock,
        relying_party=a_relying_party(
            clock,
            post_token_request=issuer.post_token_request,
            fetch_key_set=issuer.fetch_key_set,
        ),
        accounts=SqliteSubjectUserStore(database),
    )
    talk_config = a_talk_web_auth(clock)
    talk_auth = replace(
        lobby_auth,
        config=talk_config,
        relying_party=a_relying_party(
            clock,
            post_token_request=issuer.post_token_request,
            fetch_key_set=issuer.fetch_key_set,
            origin=A_TALK_ADDRESS,
        ),
        content_security_policy=talk_content_security_policy(),
    )
    lobby, _ = a_lobby_app(
        people=people,
        signing_in=SigningIn(sessions=sessions, auth=lobby_auth, decks=decks),
    )
    talk = create_talk(
        sessions=sessions,
        decks=decks,
        auth=talk_auth,
        lobby_origin=A_BROWSERS_ADDRESS,
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
