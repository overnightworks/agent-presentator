"""The pages that sign a person in and out, and the guard on everything else.

Every address but signing in, first start, and signing out answers the login
redirect while nobody is signed in, a form another site submitted is refused,
and no answer may be replayed from the browser cache (issue #8, lines 11 to 15).
"""

import posixpath
from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, Form, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse
from webauth.config import WebAuthConfig, install_web_auth_config, web_auth_config
from webauth.cookies import verify_session_cookie
from webauth.dependencies import LoginRedirect, unauthenticated_response
from webauth.login import (
    LoginOutcome,
    clear_session_cookies,
    issue_session_cookies,
    judge_credentials,
    login_attempt_budget,
)
from webauth.middleware.csrf import CsrfOriginMiddleware
from webauth.policies import CsrfPolicy, PathRules
from webauth.proxies import client_user_agent, resolve_client_ip

from presentator.api.decks import add_deck_pages
from presentator.api.hooks import HOOK_CALLS, fetch_hook
from presentator.api.pages import Pages, state_word
from presentator.api.preferences import preference_routes
from presentator.api.sources import source_routes
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.contracts.models import Account, FirstStartClosedError
from presentator.contracts.text import LobbyText

SESSION_COOKIE: Final = "presentator_session"


@dataclass
class _Credentials:
    """A writable account view `judge_credentials` can read."""

    id: str
    username: str
    role: str
    is_active: bool
    password_hash: str


def _credentials(account: Account) -> _Credentials:
    return _Credentials(
        id=account.id,
        username=account.username,
        role=account.role.value,
        is_active=account.is_active,
        password_hash=account.password_hash,
    )


_STATIC_DIR: Final = Path(__file__).parent / "static"
_STATIC_PATH: Final = "/static"
_INSIDE_STATIC: Final = f"{_STATIC_PATH}/"
_LOBBY: Final = "/"
_LOGIN: Final = "/login"
_LOGOUT: Final = "/logout"
_SETUP: Final = "/setup"
# Signing in, first start, and signing out are the only addresses that work
# without a session; logging out ends one rather than using one. The theme
# stylesheet has to render the login and setup pages themselves, so it is
# public too.
_WITHOUT_A_SESSION: Final = frozenset({_LOGIN, _LOGOUT, _SETUP})
_CSRF_POLICY: Final = CsrfPolicy(
    exempt=PathRules(prefixes=(HOOK_CALLS,)),
)
_LOGIN_REDIRECT: Final = LoginRedirect(path=_LOGIN, redirect_query_param="next")


async def _no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _is_a_stylesheet(path: str) -> bool:
    """Whether the address really stands inside the public stylesheet mount.

    The written address and its normalised form both have to: `/static/../…`
    carries the mount's prefix without standing inside it, and the guard must
    not let the way an address is written widen what it lets past.
    """
    return path.startswith(_INSIDE_STATIC) and posixpath.normpath(path).startswith(
        _INSIDE_STATIC,
    )


def _session_id_from_cookie(request: Request) -> str:
    """The raw session id a signed cookie names, or "" for none or a forged one."""
    cookie_value = request.cookies.get(SESSION_COOKIE, "")
    if not cookie_value:
        return ""
    signing_key = web_auth_config(request).signing_key
    return verify_session_cookie(cookie_value, signing_key) or ""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledAuth:
    """The library configuration this host runs the lobby with."""

    config: WebAuthConfig


@dataclass(frozen=True, slots=True, kw_only=True)
class DeckRow:
    """One deck as the list renders it: its state, name, folder, and age."""

    title: str
    slug: str
    state: str
    state_word: str
    changed: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Surfaces:
    """The lobby's HTML answers, each one asking the use cases what is true."""

    identity: Identity
    decks: Decks
    pages: Pages

    async def only_signed_in(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Send every address but the open ones to the login, and slide the window."""
        path = request.url.path
        if (
            path in _WITHOUT_A_SESSION
            or _is_a_stylesheet(path)
            or self._is_a_call_from_a_source_host(request)
        ):
            request.state.signed_in_person = None
            return await call_next(request)
        session_id = _session_id_from_cookie(request)
        person = self.identity.signed_in_user(
            session_id,
            ip_address=resolve_client_ip(request),
            user_agent=client_user_agent(request),
        )
        if person is None:
            return unauthenticated_response(request, _LOGIN_REDIRECT)
        request.state.signed_in_person = person
        request.state.signed_in_session_id = session_id
        answer = await call_next(request)
        # Every authenticated answer re-signs and re-sets the cookie, so an
        # active talk's idle window keeps sliding on the browser's own copy
        # too, not only in the session row the store holds.
        issue_session_cookies(answer, request, session_id, web_auth_config(request))
        return answer

    def _is_a_call_from_a_source_host(self, request: Request) -> bool:
        """Whether this is the sessionless, cross-origin POST the hook is for.

        Only POST under `/sources/` is open; a read of the same address, and
        every other method, stays behind the session the way any other address
        does. The prefix, never one exact path, is what the exemption matches,
        so a trailing slash cannot redirect into the guard.
        """
        return request.method == HTTPMethod.POST and request.url.path.startswith(
            HOOK_CALLS
        )

    def home(self, request: Request) -> Response:
        """List the decks the sources delivered, newest first."""
        return self.pages.page(
            request,
            "home.html",
            source_address=self.decks.source_address(),
            decks=self._rows(self.pages.appearance(request).text),
        )

    def _rows(self, text: LobbyText) -> tuple[DeckRow, ...]:
        return tuple(
            DeckRow(
                title=deck.title,
                slug=deck.slug,
                state=deck.state.value,
                state_word=state_word(deck.state, text),
                changed=self.pages.age_in_words(deck.age, text.language_tag),
            )
            for deck in self.decks.listed()
        )

    def login_page(self, request: Request) -> Response:
        """Ask for a username and a password, and offer nothing else."""
        return self._login(request, refused=False)

    def log_in(
        self,
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        """Open a session, or say the one sentence that tells nothing apart."""
        config = web_auth_config(request)
        ip_address = resolve_client_ip(request)
        if (
            login_attempt_budget(
                self.identity.attempts,
                ip_address=ip_address,
                username=username,
                config=config,
            )
            is not None
        ):
            return self._login(request, refused=True)
        account = self.identity.account_named(username)
        outcome = judge_credentials(
            password,
            None if account is None else _credentials(account),
            hasher=config.password_hasher,
        )
        if outcome is not LoginOutcome.ADMITTED or account is None:
            self.identity.note_failed_login(ip_address=ip_address, username=username)
            return self._login(request, refused=True)
        return self._signed_in(
            request,
            self.identity.open_session(
                account.as_user(),
                ip_address=ip_address,
                user_agent=client_user_agent(request),
            ),
        )

    def log_out(self, request: Request) -> Response:
        """End the session and take the cookie away."""
        self.identity.log_out(_session_id_from_cookie(request))
        answer = RedirectResponse(_LOGIN, status_code=HTTPStatus.SEE_OTHER)
        clear_session_cookies(answer, web_auth_config(request))
        return answer

    def setup_page(self, request: Request) -> Response:
        """Offer the first admin while the instance has no account."""
        if not self.identity.first_start_is_open():
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        return self._setup(request, mismatch=False)

    def set_up_admin(
        self,
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        repeated_password: Annotated[str, Form()],
    ) -> Response:
        """Create the admin the instance starts with, once."""
        if not self.identity.first_start_is_open():
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        if password != repeated_password:
            return self._setup(request, mismatch=True)
        try:
            session_id = self.identity.create_first_admin(
                username=username,
                password=password,
                ip_address=resolve_client_ip(request),
                user_agent=client_user_agent(request),
            )
        except FirstStartClosedError:
            # Another first start won the race between the count and the write.
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        return self._signed_in(request, session_id)

    def _login(self, request: Request, *, refused: bool) -> Response:
        return self.pages.page(request, "login.html", refused=refused)

    def _setup(self, request: Request, *, mismatch: bool) -> Response:
        return self.pages.page(request, "setup.html", mismatch=mismatch)

    def _signed_in(self, request: Request, session_id: str) -> Response:
        answer = RedirectResponse(_LOBBY, status_code=HTTPStatus.SEE_OTHER)
        issue_session_cookies(answer, request, session_id, web_auth_config(request))
        return answer


def create_lobby(
    *,
    identity: Identity,
    decks: Decks,
    pages: Pages,
    auth: InstalledAuth,
) -> FastAPI:
    """Build the lobby around the use cases and the adapters the host chose.

    The fetch hook always stands under `/sources/`; a call that cannot name a
    stored source and its secret is answered alike, and GET stays behind the
    session.
    """
    surfaces = _Surfaces(identity=identity, decks=decks, pages=pages)
    lobby = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_web_auth_config(lobby, auth.config)
    # The outermost middleware is added last: every answer, including the
    # guard's redirect and a refusal, carries `no-store`.
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=surfaces.only_signed_in)
    lobby.add_middleware(CsrfOriginMiddleware, policy=_CSRF_POLICY)
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=_no_store)
    lobby.add_api_route(_LOBBY, surfaces.home, methods=["GET"])
    lobby.add_api_route(_LOGIN, surfaces.login_page, methods=["GET"])
    lobby.add_api_route(_LOGIN, surfaces.log_in, methods=["POST"])
    lobby.add_api_route(_LOGOUT, surfaces.log_out, methods=["POST"])
    lobby.add_api_route(_SETUP, surfaces.setup_page, methods=["GET"])
    lobby.add_api_route(_SETUP, surfaces.set_up_admin, methods=["POST"])
    lobby.include_router(preference_routes(pages=pages))
    lobby.include_router(source_routes(pages=pages, decks=decks))
    add_deck_pages(lobby, decks=decks, pages=pages)
    lobby.mount(_STATIC_PATH, StaticFiles(directory=_STATIC_DIR), name="static")
    lobby.include_router(fetch_hook(decks=decks))
    return lobby
