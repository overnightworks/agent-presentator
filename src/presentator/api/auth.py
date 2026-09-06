"""The pages that sign a person in and out, and the guard on everything else.

Every HTML address but `/login` and `/setup` answers the login redirect, and no
answer may be replayed from the browser cache (issue #8, lines 11 to 15).
"""

from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, Form, Request, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse

from presentator.application.identity import IDLE_WINDOW, Identity
from presentator.contracts.text import LobbyText

SESSION_COOKIE: Final = "presentator_session"

_TEMPLATES: Final = Jinja2Templates(directory=Path(__file__).parent / "templates")
_LOBBY: Final = "/"
_LOGIN: Final = "/login"
_LOGOUT: Final = "/logout"
_SETUP: Final = "/setup"


class _NotSignedInError(Exception):
    """Raised by the guard, so one handler owns where an anonymous visitor goes."""


async def _no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _to_login(_request: Request, _exception: Exception) -> Response:
    return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Pages:
    """The lobby's HTML answers, each one asking the use cases what is true."""

    identity: Identity
    text: LobbyText
    secure_cookies: bool

    def home(self, request: Request) -> Response:
        """Show the lobby to whoever the cookie stands for."""
        cookie_value = request.cookies.get(SESSION_COOKIE, "")
        user = self.identity.signed_in_user(cookie_value)
        if user is None:
            raise _NotSignedInError
        answer = self._page(
            request,
            "home.html",
            person=user.username,
            log_out=self.text.log_out,
        )
        self._carry_session(answer, cookie_value)
        return answer

    def login_page(self, request: Request) -> Response:
        """Ask for a username and a password, and offer nothing else."""
        return self._login(request, refusal=None)

    def log_in(
        self,
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        """Open a session, or say the one sentence that tells nothing apart."""
        cookie_value = self.identity.log_in(username=username, password=password)
        if cookie_value is None:
            return self._login(request, refusal=self.text.login_refused)
        return self._signed_in(cookie_value)

    def log_out(self, request: Request) -> Response:
        """End the session and take the cookie away."""
        self.identity.log_out(request.cookies.get(SESSION_COOKIE, ""))
        answer = RedirectResponse(_LOGIN, status_code=HTTPStatus.SEE_OTHER)
        answer.delete_cookie(SESSION_COOKIE)
        return answer

    def setup_page(self, request: Request) -> Response:
        """Offer the first admin while the instance has no account."""
        if not self.identity.first_start_is_open():
            return RedirectResponse(_LOGIN, status_code=HTTPStatus.FOUND)
        return self._setup(request, mismatch=None)

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
            return self._setup(request, mismatch=self.text.setup_passwords_differ)
        return self._signed_in(
            self.identity.create_first_admin(username=username, password=password),
        )

    def _login(self, request: Request, *, refusal: str | None) -> Response:
        return self._page(
            request,
            "login.html",
            title=self.text.login_title,
            username=self.text.login_username,
            password=self.text.login_password,
            submit=self.text.login_submit,
            refusal=refusal,
        )

    def _setup(self, request: Request, *, mismatch: str | None) -> Response:
        return self._page(
            request,
            "setup.html",
            title=self.text.setup_title,
            once=self.text.setup_once,
            explanation=self.text.setup_explanation,
            username=self.text.setup_username,
            password=self.text.setup_password,
            repeated_password=self.text.setup_repeat_password,
            submit=self.text.setup_submit,
            mismatch=mismatch,
        )

    def _page(self, request: Request, name: str, **words: object) -> Response:
        return _TEMPLATES.TemplateResponse(
            request,
            name,
            {
                "language": self.text.language_tag,
                "wordmark": self.text.wordmark,
                **words,
            },
        )

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
    text: LobbyText,
    secure_cookies: bool,
) -> FastAPI:
    """Build the lobby around the use cases and the words the host chose."""
    pages = _Pages(identity=identity, text=text, secure_cookies=secure_cookies)
    lobby = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lobby.add_middleware(BaseHTTPMiddleware, dispatch=_no_store)
    lobby.add_exception_handler(_NotSignedInError, _to_login)
    lobby.add_api_route(_LOBBY, pages.home, methods=["GET"])
    lobby.add_api_route(_LOGIN, pages.login_page, methods=["GET"])
    lobby.add_api_route(_LOGIN, pages.log_in, methods=["POST"])
    lobby.add_api_route(_LOGOUT, pages.log_out, methods=["POST"])
    lobby.add_api_route(_SETUP, pages.setup_page, methods=["GET"])
    lobby.add_api_route(_SETUP, pages.set_up_admin, methods=["POST"])
    return lobby
