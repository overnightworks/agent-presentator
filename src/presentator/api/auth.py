"""The pages that sign a person in and out, and the guard on everything else.

Every address but signing in, first start, and signing out answers the login
redirect while nobody is signed in, a form another site submitted is refused,
and no answer may be replayed from the browser cache (issue #8, lines 11 to 15).
"""

from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, Form, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse

from presentator.api.pages import Pages
from presentator.api.preferences import preference_routes
from presentator.application.identity import IDLE_WINDOW, Identity
from presentator.application.preferences import Preferences
from presentator.contracts.models import FirstStartClosedError

SESSION_COOKIE: Final = "presentator_session"

_STATIC_DIR: Final = Path(__file__).parent / "static"
_STATIC_PATH: Final = "/static"
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


async def _same_origin_only(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """Refuse a form another site submitted.

    Containment until `webauth`'s `CsrfPolicy` arrives (ADR 0003): first start
    and login are answered without a cookie, so `SameSite=Lax` does not cover
    them, and a foreign page could otherwise create the instance's admin.
    """
    if request.method == HTTPMethod.POST and _comes_from_elsewhere(request):
        return Response(status_code=HTTPStatus.FORBIDDEN)
    return await call_next(request)


def _comes_from_elsewhere(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is not None:
        return origin != f"{request.url.scheme}://{request.url.netloc}"
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is not None and fetch_site not in _SAME_SITE_FETCHES


@dataclass(frozen=True, slots=True, kw_only=True)
class _Surfaces:
    """The sign-in answers, each one asking the use cases what is true."""

    identity: Identity
    pages: Pages
    secure_cookies: bool

    async def only_signed_in(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Send every address but the open ones to the login, and slide the window."""
        path = request.url.path
        if path in _WITHOUT_A_SESSION or path.startswith(f"{_STATIC_PATH}/"):
            request.state.signed_in_person = None
            return await call_next(request)
        cookie_value = request.cookies.get(SESSION_COOKIE, "")
        person = self.identity.signed_in_user(cookie_value)
        if person is None:
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        request.state.signed_in_person = person
        answer = await call_next(request)
        self._carry_session(answer, cookie_value)
        return answer

    def home(self, request: Request) -> Response:
        """Show the lobby to the person the guard let through."""
        return self.pages.page(request, "home.html")

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
        cookie_value = self.identity.log_in(username=username, password=password)
        if cookie_value is None:
            return self._login(request, refused=True)
        return self._signed_in(cookie_value)

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
    preferences: Preferences,
    secure_cookies: bool,
) -> FastAPI:
    """Build the lobby around the use cases and the adapters the host chose."""
    pages = Pages(preferences=preferences)
    surfaces = _Surfaces(
        identity=identity,
        pages=pages,
        secure_cookies=secure_cookies,
    )
    lobby = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # The outermost middleware is added last: every answer, including the
    # guard's redirect and a refusal, carries `no-store`.
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=surfaces.only_signed_in)
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=_same_origin_only)
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=_no_store)
    lobby.add_api_route(_LOBBY, surfaces.home, methods=["GET"])
    lobby.add_api_route(_LOGIN, surfaces.login_page, methods=["GET"])
    lobby.add_api_route(_LOGIN, surfaces.log_in, methods=["POST"])
    lobby.add_api_route(_LOGOUT, surfaces.log_out, methods=["POST"])
    lobby.add_api_route(_SETUP, surfaces.setup_page, methods=["GET"])
    lobby.add_api_route(_SETUP, surfaces.set_up_admin, methods=["POST"])
    lobby.include_router(
        preference_routes(pages=pages, preferences=preferences),
    )
    lobby.mount(_STATIC_PATH, StaticFiles(directory=_STATIC_DIR), name="static")
    return lobby
