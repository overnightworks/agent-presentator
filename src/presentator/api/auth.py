"""The pages that sign a person in and out, and the guard on everything else.

Every address but signing in, first start, and signing out answers the login
redirect while nobody is signed in, a form another site submitted is refused,
and no answer may be replayed from the browser cache (issue #8, lines 11 to 15).
"""

import posixpath
from dataclasses import dataclass
from datetime import datetime
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Annotated, Final, NoReturn

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse
from webauth.config import WebAuthConfig, install_web_auth_config, web_auth_config
from webauth.dependencies import LoginRedirect, current_user_dependency
from webauth.login import LoginOutcome, judge_credentials, login_attempt_budget
from webauth.middleware.csrf import CsrfOriginMiddleware
from webauth.policies import CsrfPolicy, PathRules
from webauth.proxies import client_user_agent, resolve_client_ip

from presentator.api.decks import add_deck_pages
from presentator.api.hooks import HOOK_CALLS, fetch_hook
from presentator.api.pages import Pages, state_word
from presentator.api.preferences import ACCOUNT, SETTINGS, THEME, preference_routes
from presentator.api.sources import source_routes
from presentator.application.decks import Decks
from presentator.application.identity import IDLE_WINDOW, Identity
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
# The asked-for path a signed-out browser is sent back to after logging in;
# the app named no query of its own for this before, so the redirect carries
# the library's default name (webauth #16).
_ASKED_FOR_PATH_PARAM: Final = "next"
_A_SESSIONLESS_STORE_ADMITTED_A_SESSION: Final = (
    "current_user_dependency admitted a session from a store that never has one"
)
_SESSION_LIFECYCLE_IS_IDENTITYS: Final = (
    "opening, touching, and ending a session stays Identity's own (ADR 0003);"
    " this store only feeds current_user_dependency its confirmed refusal"
)


class NoLiveSessions:
    """Feeds `current_user_dependency` only the shape of its own refusal.

    `Identity.signed_in_user` has already decided nobody is signed in before
    this store is ever asked (ADR 0003 keeps that decision here); a store
    that never admits a session is what makes the library's redirect-or-401
    choice — the asked-for path, the Accept negotiation — run for real
    without this app re-implementing it (issue #105).
    """

    def load(self, session_id: str) -> None:
        """Admit no session, ever, so the caller's refusal is always genuine."""
        del session_id

    def touch(
        self,
        record: object,
        *,
        ip_address: str,
        user_agent: str,
        now: datetime,
    ) -> NoReturn:
        """Refuse: `load` never admits a session for this to renew."""
        del record, ip_address, user_agent, now
        raise NotImplementedError(_SESSION_LIFECYCLE_IS_IDENTITYS)

    def create(
        self,
        user_id: str,
        expires_at: datetime,
        *,
        ip_address: str,
        user_agent: str,
    ) -> NoReturn:
        """Refuse: `Identity.open_session` is where a session begins."""
        del user_id, expires_at, ip_address, user_agent
        raise NotImplementedError(_SESSION_LIFECYCLE_IS_IDENTITYS)

    def delete(self, session_id: str) -> NoReturn:
        """Refuse: `Identity.log_out` is where a session ends."""
        del session_id
        raise NotImplementedError(_SESSION_LIFECYCLE_IS_IDENTITYS)

    def delete_for_user(self, user_id: str) -> NoReturn:
        """Refuse: this store never named a user's sessions to end them all."""
        del user_id
        raise NotImplementedError(_SESSION_LIFECYCLE_IS_IDENTITYS)

    def prune_overflow(self, user_id: str, max_sessions: int) -> NoReturn:
        """Refuse: this store never named a user's sessions to cap them."""
        del user_id, max_sessions
        raise NotImplementedError(_SESSION_LIFECYCLE_IS_IDENTITYS)


class NoAudit:
    """The identity-drift audit `current_user_dependency` asks for.

    Never reached: the confirmed-refusal call this app makes raises before
    any audit sink is read.
    """

    def session_identity_changed(self, event: object) -> NoReturn:
        """Refuse: `NoLiveSessions` never loads a session to compare drift on."""
        del event
        raise NotImplementedError(_SESSION_LIFECYCLE_IS_IDENTITYS)


_NO_LIVE_SESSIONS: Final = NoLiveSessions()
_NO_AUDIT: Final = NoAudit()
# `session_store` and `audit_sink` are FastAPI `Depends()` defaults this app
# never asks the framework to resolve: `_login_redirect_or_refusal` calls
# `current_user` directly, passing `_NO_LIVE_SESSIONS`/`_NO_AUDIT` itself, so
# these lambdas only satisfy `current_user_dependency`'s own signature.
_LOGIN_REFUSAL: Final = current_user_dependency(
    session_store=lambda: _NO_LIVE_SESSIONS,
    audit_sink=lambda: _NO_AUDIT,
    on_authenticated=lambda _user: None,
    login_redirect=LoginRedirect(_LOGIN, _ASKED_FOR_PATH_PARAM),
)
_CSRF_POLICY: Final = CsrfPolicy(
    protected=PathRules(
        exact=frozenset({_LOGIN, _LOGOUT, _SETUP, ACCOUNT, THEME}),
        prefixes=(SETTINGS,),
    ),
)


def _login_redirect_or_refusal(request: Request) -> Response:
    """The library's own choice for a request with no live session.

    A browser preferring HTML gets 302 to `/login` carrying the asked-for
    path as `next`; anything else gets 401 (webauth.dependencies.LoginRedirect).
    """
    try:
        _LOGIN_REFUSAL.current_user(
            request,
            sessions=_NO_LIVE_SESSIONS,
            audit=_NO_AUDIT,
        )
    except HTTPException as refused:
        return Response(status_code=refused.status_code, headers=refused.headers)
    raise AssertionError(_A_SESSIONLESS_STORE_ADMITTED_A_SESSION)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledAuth:
    """The library configuration and the cookie flags this host chose."""

    config: WebAuthConfig
    secure_cookies: bool


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
    secure_cookies: bool

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
        cookie_value = request.cookies.get(SESSION_COOKIE, "")
        person = self.identity.signed_in_user(
            cookie_value,
            ip_address=resolve_client_ip(request),
            user_agent=client_user_agent(request),
        )
        if person is None:
            return _login_redirect_or_refusal(request)
        request.state.signed_in_person = person
        request.state.signed_in_session_id = self.identity.cookies.session_id_from(
            cookie_value
        )
        answer = await call_next(request)
        self._set_session_cookie(request, answer, cookie_value)
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
        self.identity.log_out(request.cookies.get(SESSION_COOKIE, ""))
        answer = RedirectResponse(_LOGIN, status_code=HTTPStatus.SEE_OTHER)
        answer.delete_cookie(
            SESSION_COOKIE,
            httponly=True,
            samesite=web_auth_config(request).cookie_samesite,
            secure=self.secure_cookies,
        )
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
            cookie_value = self.identity.create_first_admin(
                username=username,
                password=password,
                ip_address=resolve_client_ip(request),
                user_agent=client_user_agent(request),
            )
        except FirstStartClosedError:
            # Another first start won the race between the count and the write.
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        return self._signed_in(request, cookie_value)

    def _login(self, request: Request, *, refused: bool) -> Response:
        return self.pages.page(request, "login.html", refused=refused)

    def _setup(self, request: Request, *, mismatch: bool) -> Response:
        return self.pages.page(request, "setup.html", mismatch=mismatch)

    def _signed_in(self, request: Request, cookie_value: str) -> Response:
        answer = RedirectResponse(_LOBBY, status_code=HTTPStatus.SEE_OTHER)
        self._set_session_cookie(request, answer, cookie_value)
        return answer

    def _set_session_cookie(
        self,
        request: Request,
        answer: Response,
        cookie_value: str,
    ) -> None:
        """Hand the browser its cookie, `SameSite` read from the installed config.

        Every authenticated answer re-sets it, so an active talk's idle
        window keeps sliding on the browser's own copy too, not only in the
        session row the store holds.
        """
        answer.set_cookie(
            SESSION_COOKIE,
            cookie_value,
            max_age=int(IDLE_WINDOW.total_seconds()),
            httponly=True,
            samesite=web_auth_config(request).cookie_samesite,
            secure=self.secure_cookies,
        )


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
    surfaces = _Surfaces(
        identity=identity,
        decks=decks,
        pages=pages,
        secure_cookies=auth.secure_cookies,
    )
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
