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

from fastapi import APIRouter, FastAPI, Form, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse
from webauth.config import WebAuthConfig, install_web_auth_config, web_auth_config
from webauth.login import LoginOutcome, judge_credentials, login_attempt_budget
from webauth.proxies import client_user_agent, request_is_https, resolve_client_ip

from presentator.api.decks import add_deck_pages
from presentator.api.hooks import HOOK_CALLS
from presentator.api.pages import Pages, state_word
from presentator.api.preferences import preference_routes
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
_SAME_SITE_FETCHES: Final = frozenset({"same-origin", "same-site", "none"})


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


def _comes_from_elsewhere(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is not None:
        scheme = "https" if request_is_https(request) else "http"
        return origin != f"{scheme}://{request.url.netloc}"
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is not None and fetch_site not in _SAME_SITE_FETCHES


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
    hook_is_armed: bool

    async def same_origin_only(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Refuse a form another site submitted.

        `webauth.middleware.csrf.CsrfOriginMiddleware` checks Origin against a
        host allowlist and does not read Sec-Fetch-Site, so a cross-site POST
        this instance's own Origin header could not describe would pass it.
        The ruled refusal is Origin and Sec-Fetch-Site against this request's
        own origin (issue #8), which is what this guard still does.
        """
        if self._is_a_foreign_form(request):
            return Response(status_code=HTTPStatus.FORBIDDEN)
        return await call_next(request)

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
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        request.state.signed_in_person = person
        answer = await call_next(request)
        self._carry_session(answer, cookie_value)
        return answer

    def _is_a_foreign_form(self, request: Request) -> bool:
        if request.method != HTTPMethod.POST:
            return False
        if self._is_a_call_from_a_source_host(request):
            return False
        return _comes_from_elsewhere(request)

    def _is_a_call_from_a_source_host(self, request: Request) -> bool:
        """Whether this is the sessionless, cross-origin POST the hook is for.

        Only that one call is open, and only while a secret arms the hook; a
        read of the same address, and every other method, stays behind the
        session the way any other address does.
        """
        return (
            self.hook_is_armed
            and request.method == HTTPMethod.POST
            and request.url.path.startswith(HOOK_CALLS)
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
            samesite="lax",
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
        return self._signed_in(cookie_value)

    def _login(self, request: Request, *, refused: bool) -> Response:
        return self.pages.page(request, "login.html", refused=refused)

    def _setup(self, request: Request, *, mismatch: bool) -> Response:
        return self.pages.page(request, "setup.html", mismatch=mismatch)

    def _signed_in(self, cookie_value: str) -> Response:
        answer = RedirectResponse(_LOBBY, status_code=HTTPStatus.SEE_OTHER)
        self._carry_session(answer, cookie_value)
        return answer

    def _carry_session(self, answer: Response, cookie_value: str) -> None:
        answer.set_cookie(
            SESSION_COOKIE,
            cookie_value,
            max_age=int(IDLE_WINDOW.total_seconds()),
            httponly=True,
            samesite="lax",
            secure=self.secure_cookies,
        )


def create_lobby(
    *,
    identity: Identity,
    decks: Decks,
    pages: Pages,
    auth: InstalledAuth,
    fetch_hook: APIRouter | None,
) -> FastAPI:
    """Build the lobby around the use cases and the adapters the host chose.

    Without a fetch hook the instance has no address a source's host may call,
    and nothing below `/hooks/` leaves the session guard.
    """
    surfaces = _Surfaces(
        identity=identity,
        decks=decks,
        pages=pages,
        secure_cookies=auth.secure_cookies,
        hook_is_armed=fetch_hook is not None,
    )
    lobby = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_web_auth_config(lobby, auth.config)
    # The outermost middleware is added last: every answer, including the
    # guard's redirect and a refusal, carries `no-store`.
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=surfaces.only_signed_in)
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=surfaces.same_origin_only)
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
    if fetch_hook is not None:
        lobby.include_router(fetch_hook)
    return lobby
