"""The pages that sign a person in and out, and the session gate on everything else.

webauth's `SessionGate` decides every request: a gated address
answers the redirect to the start of the sign-in (or 401 to a JSON caller) while
nobody is signed in, the public and optional addresses below are the
exceptions, a form another site submitted is refused, and no answer may be
replayed from the browser cache (issue #8, lines 11 to 15).
"""

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Final, assert_never, cast

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from webauth.config import WebAuthConfig, install_web_auth_config, web_auth_config
from webauth.cookies import issue_session_cookies
from webauth.dependencies import (
    ConnectionRefusal,
    LoginRedirect,
    admit_session,
    session_id_from_request,
)
from webauth.middleware.session_gate import SessionGate
from webauth.middleware.stack import install_web_auth_middleware
from webauth.oidc import OidcRefusal, OidcRelyingParty
from webauth.oidc_login import (
    AdmissionConfig,
    AdmissionRefusal,
    AdmittedLogin,
    BackchannelLogoutResult,
    finish_login,
    log_out,
    open_session,
    receive_backchannel_logout,
    start_login,
)
from webauth.policies import (
    BodySizePolicy,
    BodySizeRule,
    CacheControlRule,
    CspDirective,
    CsrfPolicy,
    PathRules,
    PublicRule,
    SecurityHeadersPolicy,
    default_content_security_policy,
)
from webauth.ports import (
    AuditSink,
    SessionRecordStore,
    SubjectUserStore,
    WriteLock,
)

from presentator.api.copresenter import CoPresenterSurface, add_copresenter_routes
from presentator.api.decks import add_deck_pages
from presentator.api.hooks import HOOK_CALLS, fetch_hook
from presentator.api.legal import DATENSCHUTZ, Emphasis
from presentator.api.pages import (
    TEMPLATES,
    Pages,
    gated_person,
    state_word,
    user_of_admitted,
)
from presentator.api.preferences import preference_routes
from presentator.api.sources import source_routes
from presentator.application.decks import Decks
from presentator.application.voice import VoiceStatusUse
from presentator.contracts.models import User
from presentator.contracts.text import LobbyText

_logger = logging.getLogger(__name__)

_STATIC_DIR: Final = Path(__file__).parent / "static"
_STATIC_PATH: Final = "/static"
_INSIDE_STATIC: Final = f"{_STATIC_PATH}/"
_LOBBY: Final = "/"
LOGIN_PATH: Final = "/login"
_START_SIGN_IN: Final = "/login/oidc"
SIGN_IN_CALLBACK_PATH: Final = "/auth/callback"
BACKCHANNEL_LOGOUT_PATH: Final = "/auth/backchannel-logout"
LOGOUT_PATH: Final = "/logout"
_DATENSCHUTZ: Final = "/datenschutz"
_SETUP: Final = "/setup"
# Signing in (its start and the issuer's return) and signing out are the
# addresses the gate leaves alone; logging out ends a session rather than
# using one. The theme stylesheet has to render the open pages themselves, so
# it is public too, and so are the two addresses another server posts to: a
# source host's POST under the hook prefix and Keycloak's back-channel logout.
# `/setup` is a retired address: it answers 404 to everyone, so a signed-out
# reader is not sent to a login for a page that does not exist.
# The Datenschutzerklärung has to be readable before anyone signs in (ADR
# 0015), yet a reader who is signed in keeps the header there like on every
# other page.
_SERVER_CALLS: Final = PathRules(
    exact=frozenset({BACKCHANNEL_LOGOUT_PATH}),
    prefixes=(HOOK_CALLS,),
)
_PUBLIC_RULES: Final = (
    PublicRule(
        PathRules(
            exact=frozenset(
                {
                    LOGIN_PATH,
                    _START_SIGN_IN,
                    SIGN_IN_CALLBACK_PATH,
                    LOGOUT_PATH,
                    _SETUP,
                }
            )
        )
    ),
    PublicRule(PathRules(prefixes=(_INSIDE_STATIC,))),
    PublicRule(_SERVER_CALLS, methods=frozenset({HTTPMethod.POST})),
)
_OPTIONAL_PATHS: Final = PathRules(exact=frozenset({_DATENSCHUTZ}))
PERMISSIONS_POLICY: Final = "camera=(), microphone=(self), geolocation=()"
_CSRF_POLICY: Final = CsrfPolicy(exempt=_SERVER_CALLS)
_COPRESENTER_CSRF_POLICY: Final = CsrfPolicy(
    protected=PathRules(exact=frozenset({"/copresenter/ask"})),
)
# Every route has this budget, forms and the ask included; the hook keeps a git
# host's push budget, since GitHub documents payloads of up to 25 MB.
_BODY_SIZE: Final = BodySizePolicy(
    default_max_bytes=32 * 1024,
    rules=(
        BodySizeRule(
            PathRules(prefixes=(HOOK_CALLS,)),
            max_bytes=25 * 1024 * 1024,
            methods=frozenset({HTTPMethod.POST}),
        ),
    ),
)
_LOGIN_REDIRECT: Final = LoginRedirect(
    path=_START_SIGN_IN,
    redirect_query_param="next",
)
# Jinja's stubs type the globals as its own defaults only, so the dict is
# named as the mixed bag it is.
cast("dict[str, object]", TEMPLATES.env.globals).update(
    login_path=LOGIN_PATH,
    logout_path=LOGOUT_PATH,
)


def content_security_policy(end_session_origin: str) -> str:
    """The one policy every answer of the lobby carries.

    Nothing in the lobby or a built talk needs an inline script, an external
    script host, or a frame: every asset ships under `/static` or a deck's own
    build directory, both served from this origin. A built Slidev talk inlines
    styles, which the library's default policy does not admit. Signing out is a
    form the browser follows to the issuer, and Chromium checks `form-action`
    on that redirect, so the end-session endpoint's origin is the one host a
    form may reach.
    """
    return default_content_security_policy(
        [],
        added_sources={
            CspDirective.STYLE_SRC: ["'unsafe-inline'"],
            CspDirective.FORM_ACTION: [end_session_origin],
        },
    )


def carry_set_cookies(source: Response, target: Response) -> None:
    """Copy every `Set-Cookie` of one answer to another, and no other header."""
    for cookie in source.headers.getlist("set-cookie"):
        target.headers.append("set-cookie", cookie)


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
    connection: HTTPConnection,
    sessions: SessionRecordStore,
) -> SignedInConnection | None:
    """Resolve one HTTP or WebSocket connection through the library's one decider."""
    config = web_auth_config(connection)
    session_id = session_id_from_request(connection, config)
    if session_id is None:
        return None
    admitted = admit_session(session_id, sessions=sessions, config=config)
    if isinstance(admitted, ConnectionRefusal):
        return None
    return SignedInConnection(person=user_of_admitted(admitted), session_id=session_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledAuth:
    """The library configuration this host runs the lobby with."""

    config: WebAuthConfig
    relying_party: OidcRelyingParty
    admission: AdmissionConfig
    accounts: SubjectUserStore
    lock: WriteLock
    audit: AuditSink
    content_security_policy: str


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

    sessions: SessionRecordStore
    decks: Decks
    pages: Pages
    auth: InstalledAuth

    def home(self, request: Request) -> Response:
        """List the signed-in person's decks, newest first."""
        viewer = gated_person(request).id
        return self.pages.page(
            request,
            "home.html",
            source_address=self.decks.source_address(viewer=viewer),
            decks=self._rows(self.pages.appearance(request).text, viewer),
        )

    def _rows(self, text: LobbyText, viewer: str) -> tuple[DeckRow, ...]:
        return tuple(
            DeckRow(
                title=deck.title,
                slug=deck.slug,
                state=deck.state.value,
                state_word=state_word(deck.state, text),
                changed=self.pages.age_in_words(deck.age, text.language_tag),
            )
            for deck in self.decks.listed(viewer=viewer)
        )

    def login_page(self, request: Request) -> Response:
        """Show the signed-out page, whose one button starts the sign-in."""
        return self.pages.page(request, "login.html", start_sign_in=_START_SIGN_IN)

    def start_sign_in(self, request: Request) -> Response:
        """Send the browser to the issuer, remembering where it was headed.

        `next` is only a request to return somewhere: the library keeps it
        when it is a path of this app and drops anything else.
        """
        answer = Response(status_code=HTTPStatus.SEE_OTHER)
        answer.headers["location"] = start_login(
            self.auth.relying_party,
            request,
            answer,
            web_auth_config(request),
            return_to=request.query_params.get(_LOGIN_REDIRECT.redirect_query_param),
        )
        return answer

    def sign_in_callback(self, request: Request) -> Response:
        """Complete the sign-in the issuer returns to, or show why it failed.

        The `Response` the library writes its cookies to is the one answered
        with, so the pending login's clear and the logout hint travel with the
        session cookies.
        """
        config = web_auth_config(request)
        answer = Response()
        outcome = finish_login(
            self.auth.relying_party,
            request,
            answer,
            config,
            admission=self.auth.admission,
            accounts=self.auth.accounts,
            lock=self.auth.lock,
            audit=self.auth.audit,
        )
        match outcome:
            case AdmittedLogin():
                return self._signed_in(request, answer, outcome)
            case AdmissionRefusal.NOT_A_MEMBER:
                return self._refusal(request, "no_access.html", kept=answer)
            case (
                OidcRefusal()
                | AdmissionRefusal.FOREIGN_ISSUER
                | AdmissionRefusal.USERNAME_TAKEN
            ):
                return self._refusal(request, "login_failed.html", kept=answer)
            case _:
                assert_never(outcome)

    def _signed_in(
        self,
        request: Request,
        answer: Response,
        login: AdmittedLogin,
    ) -> Response:
        """Open the session and send the browser on, with every cookie on `answer`."""
        config = web_auth_config(request)
        session = open_session(
            request,
            config,
            login,
            sessions=self.sessions,
            lock=self.auth.lock,
            max_sessions=None,
        )
        issue_session_cookies(answer, session.id, config)
        answer.status_code = HTTPStatus.SEE_OTHER
        answer.headers["location"] = login.return_to or _LOBBY
        return answer

    def _refusal(self, request: Request, template: str, *, kept: Response) -> Response:
        """The page a refused sign-in shows, with the cookies the library kept."""
        page = self.pages.page(
            request,
            template,
            status=HTTPStatus.FORBIDDEN,
            start_sign_in=_START_SIGN_IN,
        )
        carry_set_cookies(kept, page)
        return page

    def log_out(self, request: Request) -> Response:
        """End the session here and at the issuer, and clear the sign-in's cookies.

        The answer is one `Response`, never a `RedirectResponse`, so the
        library's cookie deletions survive the redirect it points at the issuer.
        """
        answer = Response(status_code=HTTPStatus.SEE_OTHER)
        answer.headers["location"] = log_out(
            self.auth.relying_party,
            request,
            answer,
            web_auth_config(request),
            sessions=self.sessions,
            lock=self.auth.lock,
        )
        return answer

    async def backchannel_logout(self, request: Request) -> Response:
        """End every session of the person Keycloak's Logout Token names.

        The body is read outside any catch-all, so a streamed body over the
        cap reaches the cap's middleware and gets its 413. Everything else
        that goes wrong answers 400, the profile's refusal.
        """
        body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            result = await run_in_threadpool(
                self._end_the_sessions_of, body, content_type
            )
        except Exception:
            _logger.exception("back-channel logout failed")
            result = BackchannelLogoutResult.REFUSED
        status = (
            HTTPStatus.OK
            if result is BackchannelLogoutResult.ACCEPTED
            else HTTPStatus.BAD_REQUEST
        )
        return Response(status_code=status)

    def _end_the_sessions_of(
        self, body: bytes, content_type: str | None
    ) -> BackchannelLogoutResult:
        return receive_backchannel_logout(
            self.auth.relying_party,
            body,
            content_type,
            accounts=self.auth.accounts,
            sessions=self.sessions,
            lock=self.auth.lock,
            audit=self.auth.audit,
        )

    def datenschutz_page(self, request: Request) -> Response:
        """Show the Datenschutzerklärung to anyone, signed in or not."""
        return self.pages.page(
            request,
            "datenschutz.html",
            document=DATENSCHUTZ,
            emphasis=Emphasis,
        )


def _install_middleware(
    lobby: FastAPI,
    content_security_policy: str,
    sessions: SessionRecordStore,
) -> None:
    """Install the library's stack once, in its own order.

    Every answer, including the gate's redirect and a refusal, carries the
    security headers. Each store call commits on its own and `touch` is one
    update-only statement (ADR 0003), so the gate's admission takes no lock.
    """
    install_web_auth_middleware(
        lobby,
        security_headers=SecurityHeadersPolicy(
            content_security_policy=content_security_policy,
            permissions_policy=PERMISSIONS_POLICY,
            cache_control_rules=(
                CacheControlRule(PathRules(prefixes=("/",)), "no-store"),
            ),
        ),
        body_size=_BODY_SIZE,
        csrf_origin=_CSRF_POLICY,
        csrf_token=_COPRESENTER_CSRF_POLICY,
        session_gate=SessionGate(
            session_transaction=lambda: nullcontext(sessions),
            login_redirect=_LOGIN_REDIRECT,
            public=_PUBLIC_RULES,
            optional=_OPTIONAL_PATHS,
        ),
    )


def create_lobby(
    *,
    sessions: SessionRecordStore,
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
    surfaces = _Surfaces(
        sessions=sessions,
        decks=decks,
        pages=pages,
        auth=auth,
    )
    lobby = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_web_auth_config(lobby, auth.config)
    _install_middleware(lobby, auth.content_security_policy, sessions)
    lobby.add_api_route(_LOBBY, surfaces.home, methods=["GET"])
    lobby.add_api_route(LOGIN_PATH, surfaces.login_page, methods=["GET"])
    lobby.add_api_route(_START_SIGN_IN, surfaces.start_sign_in, methods=["GET"])
    lobby.add_api_route(
        SIGN_IN_CALLBACK_PATH, surfaces.sign_in_callback, methods=["GET"]
    )
    lobby.add_api_route(LOGOUT_PATH, surfaces.log_out, methods=["POST"])
    lobby.add_api_route(
        BACKCHANNEL_LOGOUT_PATH, surfaces.backchannel_logout, methods=["POST"]
    )
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
            admission=lambda connection: signed_in_connection(connection, sessions),
        )
    return lobby
