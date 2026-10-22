"""The talk host: an origin that signs people in by itself and serves their talks.

A deck is code, so its talk runs on a site of its own where the lobby's cookies
never reach. The talk host answers no lobby route: it starts the sign-in at the
same issuer, completes it at its own callback, and serves the signed-in
owner's built talk. It has no page of its own, so a refused sign-in is answered
by a redirect to the lobby's refusal route.
"""

from contextlib import nullcontext
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, Request, Response
from webauth.config import install_web_auth_config
from webauth.middleware.session_gate import SessionGate
from webauth.middleware.stack import install_web_auth_middleware
from webauth.policies import (
    BodySizePolicy,
    CacheControlRule,
    CspDirective,
    CsrfPolicy,
    PathRules,
    PublicRule,
    SecurityHeadersPolicy,
    default_content_security_policy,
)
from webauth.ports import SessionRecordStore

from presentator.api.auth import (
    DEFAULT_BODY_BYTES,
    LOGIN_REDIRECT,
    SIGN_IN_CALLBACK_PATH,
    START_SIGN_IN_PATH,
    InstalledAuth,
    SignIn,
    TalkRefusal,
    carry_set_cookies,
    talk_refusal_path,
)
from presentator.api.decks import add_talk_mount
from presentator.application.decks import Decks

TALK_PERMISSIONS_POLICY: Final = "camera=(), microphone=(), geolocation=()"
_ROOT: Final = "/"
_SIGN_IN_ADDRESSES: Final = PathRules(
    exact=frozenset({_ROOT, START_SIGN_IN_PATH, SIGN_IN_CALLBACK_PATH}),
)


def talk_content_security_policy() -> str:
    """The one policy every answer of the talk host carries.

    A built Slidev talk inlines its styles and loads everything else from its
    own build directory, so no other host is named: no font host, no form
    target, no frame.
    """
    return default_content_security_policy(
        [],
        added_sources={CspDirective.STYLE_SRC: ["'unsafe-inline'"]},
    )


def create_talk(
    *,
    sessions: SessionRecordStore,
    decks: Decks,
    auth: InstalledAuth,
    lobby_origin: str,
) -> FastAPI:
    """Build the talk host around the same stores and use cases as the lobby."""
    sign_in = SignIn(sessions=sessions, auth=auth)

    def refused(refusal: TalkRefusal, kept: Response) -> Response:
        answer = Response(status_code=HTTPStatus.SEE_OTHER)
        answer.headers["location"] = f"{lobby_origin}{talk_refusal_path(refusal)}"
        carry_set_cookies(kept, answer)
        return answer

    def finish_sign_in(request: Request) -> Response:
        return sign_in.finish(request, refused=refused)

    def to_the_lobby() -> Response:
        answer = Response(status_code=HTTPStatus.SEE_OTHER)
        answer.headers["location"] = lobby_origin
        return answer

    talk = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_web_auth_config(talk, auth.config)
    install_web_auth_middleware(
        talk,
        security_headers=SecurityHeadersPolicy(
            content_security_policy=auth.content_security_policy,
            permissions_policy=TALK_PERMISSIONS_POLICY,
            cache_control_rules=(
                CacheControlRule(PathRules(prefixes=(_ROOT,)), "no-store"),
            ),
        ),
        body_size=BodySizePolicy(default_max_bytes=DEFAULT_BODY_BYTES),
        csrf_origin=CsrfPolicy(),
        session_gate=SessionGate(
            session_transaction=lambda: nullcontext(sessions),
            login_redirect=LOGIN_REDIRECT,
            public=(PublicRule(_SIGN_IN_ADDRESSES),),
        ),
    )
    talk.add_api_route(_ROOT, to_the_lobby, methods=["GET"])
    talk.add_api_route(START_SIGN_IN_PATH, sign_in.start, methods=["GET"])
    talk.add_api_route(SIGN_IN_CALLBACK_PATH, finish_sign_in, methods=["GET"])
    add_talk_mount(talk, decks=decks)
    return talk
