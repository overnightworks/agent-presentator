"""The pages that sign a person in and out, and the guard on everything else.

Every address but signing in, signing out, and the Datenschutzerklärung answers
the login redirect while nobody is signed in, a form another site submitted is
refused, and no answer may be replayed from the browser cache (issue #8, lines
11 to 15).
"""

import posixpath
from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import HTTPConnection
from starlette.responses import RedirectResponse
from webauth.config import WebAuthConfig, install_web_auth_config, web_auth_config
from webauth.cookies import clear_session_cookies, issue_session_cookies
from webauth.dependencies import (
    LoginRedirect,
    session_id_from_request,
    unauthenticated_response,
)
from webauth.middleware.csrf import CsrfOriginMiddleware, CsrfTokenMiddleware
from webauth.middleware.security_headers import SecurityHeadersMiddleware
from webauth.policies import (
    CacheControlRule,
    CspDirective,
    CsrfPolicy,
    PathRules,
    SecurityHeadersPolicy,
    default_content_security_policy,
)
from webauth.proxies import client_user_agent, resolve_client_ip

from presentator.api.copresenter import CoPresenterSurface, add_copresenter_routes
from presentator.api.decks import add_deck_pages
from presentator.api.hooks import HOOK_CALLS, fetch_hook
from presentator.api.legal import DATENSCHUTZ, Emphasis
from presentator.api.pages import Pages, state_word
from presentator.api.preferences import preference_routes
from presentator.api.sources import source_routes
from presentator.application.decks import Decks
from presentator.application.identity import Identity
from presentator.application.voice import VoiceStatusUse
from presentator.contracts.models import User
from presentator.contracts.text import LobbyText

_STATIC_DIR: Final = Path(__file__).parent / "static"
_STATIC_PATH: Final = "/static"
_INSIDE_STATIC: Final = f"{_STATIC_PATH}/"
_LOBBY: Final = "/"
_LOGIN: Final = "/login"
_LOGOUT: Final = "/logout"
_DATENSCHUTZ: Final = "/datenschutz"
_SETUP: Final = "/setup"
# Signing in and signing out are the addresses the guard leaves
# alone; logging out ends a session rather than using one. The theme
# stylesheet has to render the open pages themselves, so it is public too.
# `/setup` is a retired address: it answers 404 to everyone, so a signed-out
# reader is not sent to a login for a page that does not exist.
_WITHOUT_A_SESSION: Final = frozenset({_LOGIN, _LOGOUT, _SETUP})
# The Datenschutzerklärung has to be readable before anyone signs in (ADR
# 0015), yet a reader who is signed in keeps the header and the sliding window
# there like on every other page.
_OPEN_TO_A_SIGNED_OUT_READER: Final = frozenset({_DATENSCHUTZ})
_CSRF_POLICY: Final = CsrfPolicy(
    exempt=PathRules(prefixes=(HOOK_CALLS,)),
)
_COPRESENTER_CSRF_POLICY: Final = CsrfPolicy(
    protected=PathRules(exact=frozenset({"/copresenter/ask"})),
)
_LOGIN_REDIRECT: Final = LoginRedirect(path=_LOGIN, redirect_query_param="next")
# Nothing in the lobby or a built talk needs an inline script, an external
# script host, or a frame: every asset ships under `/static` or a deck's own
# build directory, both served from this origin. A built Slidev talk inlines
# styles and loads its fonts from Google Fonts, which the library's default
# policy does not admit. The microphone grant is the co-presenter's own: never the
# camera, never a page's location.
CONTENT_SECURITY_POLICY: Final = default_content_security_policy(
    [],
    added_sources={
        CspDirective.STYLE_SRC: ["'unsafe-inline'", "https://fonts.googleapis.com"],
        CspDirective.FONT_SRC: ["https://fonts.gstatic.com"],
    },
)
_SECURITY_HEADERS: Final = SecurityHeadersPolicy(
    content_security_policy=CONTENT_SECURITY_POLICY,
    permissions_policy="camera=(), microphone=(self), geolocation=()",
    cache_control_rules=(CacheControlRule(PathRules(prefixes=("/",)), "no-store"),),
)


def _is_a_stylesheet(path: str) -> bool:
    """Whether the address really stands inside the public stylesheet mount.

    The written address and its normalised form both have to: `/static/../…`
    carries the mount's prefix without standing inside it, and the guard must
    not let the way an address is written widen what it lets past.
    """
    return path.startswith(_INSIDE_STATIC) and posixpath.normpath(path).startswith(
        _INSIDE_STATIC,
    )


def _session_id_from_cookie(connection: HTTPConnection) -> str:
    """The raw session id the one signed cookie names, or "" for none.

    A forged, oversized, or doubled cookie names none either: the library
    refuses to pick between two same-named cookies.
    """
    return session_id_from_request(connection, web_auth_config(connection)) or ""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignedInConnection:
    """The hash-free person and authoritative session behind one connection."""

    person: User
    session_id: str

    @property
    def is_admin(self) -> bool:
        """Whether the signed-in person holds the admin role."""
        return self.person.is_admin


def signed_in_connection(
    request: HTTPConnection,
    identity: Identity,
) -> SignedInConnection | None:
    """Resolve one HTTP or WebSocket connection through the existing login owner."""
    session_id = _session_id_from_cookie(request)
    person = identity.signed_in_user(
        session_id,
        ip_address=resolve_client_ip(request),
        user_agent=client_user_agent(request),
    )
    if person is None:
        return None
    return SignedInConnection(person=person, session_id=session_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledAuth:
    """The library configuration this host runs the lobby with."""

    config: WebAuthConfig


@dataclass(frozen=True, slots=True, kw_only=True)
class LobbyPrivate:
    """Private capabilities the authenticated lobby may expose to an admin."""

    voice: VoiceStatusUse
    copresenter: CoPresenterSurface | None = None


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
        connection = signed_in_connection(request, self.identity)
        if connection is None:
            if path in _OPEN_TO_A_SIGNED_OUT_READER:
                request.state.signed_in_person = None
                return await call_next(request)
            return unauthenticated_response(request, _LOGIN_REDIRECT)
        request.state.signed_in_person = connection.person
        request.state.signed_in_session_id = connection.session_id
        answer = await call_next(request)
        # Every authenticated answer re-signs and re-sets the cookie, so an
        # active talk's idle window keeps sliding on the browser's own copy
        # too, not only in the session row the store holds.
        issue_session_cookies(
            answer,
            connection.session_id,
            web_auth_config(request),
        )
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
        """Show the sign-in page, which offers no way in yet."""
        return self.pages.page(request, "login.html")

    def log_out(self, request: Request) -> Response:
        """End the session and take the cookie away."""
        self.identity.log_out(_session_id_from_cookie(request))
        answer = RedirectResponse(_LOGIN, status_code=HTTPStatus.SEE_OTHER)
        clear_session_cookies(answer, web_auth_config(request))
        return answer

    def datenschutz_page(self, request: Request) -> Response:
        """Show the Datenschutzerklärung to anyone, signed in or not."""
        return self.pages.page(
            request,
            "datenschutz.html",
            document=DATENSCHUTZ,
            emphasis=Emphasis,
        )


def create_lobby(
    *,
    identity: Identity,
    decks: Decks,
    pages: Pages,
    auth: InstalledAuth,
    private: LobbyPrivate,
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
    # guard's redirect and a refusal, carries the security headers.
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=surfaces.only_signed_in)
    lobby.add_middleware(CsrfOriginMiddleware, policy=_CSRF_POLICY)
    lobby.add_middleware(CsrfTokenMiddleware, policy=_COPRESENTER_CSRF_POLICY)
    lobby.add_middleware(SecurityHeadersMiddleware, policy=_SECURITY_HEADERS)
    lobby.add_api_route(_LOBBY, surfaces.home, methods=["GET"])
    lobby.add_api_route(_LOGIN, surfaces.login_page, methods=["GET"])
    lobby.add_api_route(_LOGOUT, surfaces.log_out, methods=["POST"])
    lobby.add_api_route(_DATENSCHUTZ, surfaces.datenschutz_page, methods=["GET"])
    lobby.include_router(preference_routes(pages=pages, voice=private.voice))
    lobby.include_router(source_routes(pages=pages, decks=decks))
    add_deck_pages(lobby, decks=decks, pages=pages)
    lobby.mount(_STATIC_PATH, StaticFiles(directory=_STATIC_DIR), name="static")
    lobby.include_router(fetch_hook(decks=decks))
    if private.copresenter is not None:
        add_copresenter_routes(
            lobby,
            surface=private.copresenter,
            admission=lambda connection: signed_in_connection(connection, identity),
        )
    return lobby
