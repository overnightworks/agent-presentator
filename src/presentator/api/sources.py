"""Settings · Sources: the list of what this instance reads, and adding one.

Admin only, refused exactly as Settings · General is. No access secret appears
here. The webhook secret appears on the created screen exactly once.
"""

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Annotated, Final

from fastapi import APIRouter, Form, Request, Response
from starlette.responses import RedirectResponse
from webauth.proxies import request_is_https

from presentator.api.hooks import hook_address
from presentator.api.pages import Pages
from presentator.api.preferences import SETTINGS
from presentator.application.decks import AddedSource, Decks, SourceRefusal
from presentator.contracts.decks import AccessKind, SourceState
from presentator.contracts.models import User
from presentator.contracts.text import LobbyText

SOURCES: Final = f"{SETTINGS}/sources"
NEW: Final = f"{SOURCES}/new"
CREATED: Final = f"{SOURCES}/{{name}}"
FETCH: Final = f"{SOURCES}/{{name}}/fetch"
_HTTPS_ACCESS: Final = "https"
_SSH_ACCESS: Final = "ssh"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRow:
    """One source as the list renders it: state, name, URL, access, fetched age."""

    name: str
    url: str
    access: str | None
    state: str
    state_word: str
    fetched: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDraft:
    """What the Add source form shows after a refusal: everything but the secret."""

    name: str
    url: str
    access: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Surfaces:
    """The Sources pages: the list, the form, the created screen, and Fetch now."""

    pages: Pages
    decks: Decks
    _shown_once: dict[str, str] = field(default_factory=dict[str, str])

    def sources_page(self, request: Request) -> Response:
        """Show each source and what its newest run said, to an admin."""
        if not _signed_in(request).is_admin:
            return _refused()
        text = self.pages.appearance(request).text
        return self.pages.page(
            request,
            "sources.html",
            sources=self._rows(text),
        )

    def add_page(self, request: Request) -> Response:
        """Offer the form that creates a source, to an admin."""
        if not _signed_in(request).is_admin:
            return _refused()
        return self._form(request)

    def create_source(
        self,
        request: Request,
        name: Annotated[str, Form()] = "",
        url: Annotated[str, Form()] = "",
        access: Annotated[str, Form()] = _HTTPS_ACCESS,
        secret: Annotated[str, Form()] = "",
    ) -> Response:
        """Store the source, fetch it once, and show the webhook secret once."""
        person = _signed_in(request)
        if not person.is_admin:
            return _refused()
        added = self.decks.add_source(
            name=name,
            url=url,
            access=access,
            secret=secret,
            owner_id=person.id,
        )
        if not isinstance(added, AddedSource):
            return self._form(
                request,
                draft=SourceDraft(
                    name=name,
                    url=url,
                    access=access or _HTTPS_ACCESS,
                    reason=_refusal_sentence(
                        added,
                        self.pages.appearance(request).text,
                    ),
                ),
            )
        self._shown_once[added.source.name] = added.webhook_secret
        return RedirectResponse(
            f"{SOURCES}/{added.source.name}",
            status_code=HTTPStatus.SEE_OTHER,
        )

    def created_page(self, request: Request, name: str) -> Response:
        """Show the webhook address, and the secret only the first time."""
        if not _signed_in(request).is_admin:
            return _refused()
        if not any(source.name == name for source in self.decks.listed_sources()):
            return Response(status_code=HTTPStatus.NOT_FOUND)
        scheme = "https" if request_is_https(request) else request.url.scheme
        return self.pages.page(
            request,
            "source_created.html",
            source_name=name,
            address=f"{scheme}://{request.url.netloc}{hook_address(name)}",
            webhook_secret=self._shown_once.pop(name, None),
        )

    def fetch_now(self, request: Request, name: str) -> Response:
        """Refresh that one source and return to the list."""
        if not _signed_in(request).is_admin:
            return _refused()
        if not self.decks.refresh_named(name):
            return Response(status_code=HTTPStatus.NOT_FOUND)
        return RedirectResponse(SOURCES, status_code=HTTPStatus.SEE_OTHER)

    def _form(
        self,
        request: Request,
        draft: SourceDraft | None = None,
    ) -> Response:
        return self.pages.page(
            request,
            "source_new.html",
            draft=draft,
            https_access=_HTTPS_ACCESS,
            ssh_access=_SSH_ACCESS,
        )

    def _rows(self, text: LobbyText) -> tuple[SourceRow, ...]:
        return tuple(
            SourceRow(
                name=source.name,
                url=source.url,
                access=_access_word(source.access, text),
                state=source.state.value,
                state_word=_state_word(source.state, text),
                fetched=(
                    None
                    if source.age is None
                    else self.pages.age_in_words(source.age, text.language_tag)
                ),
            )
            for source in self.decks.listed_sources()
        )


def source_routes(*, pages: Pages, decks: Decks) -> APIRouter:
    """The Sources addresses, for the lobby factory to include."""
    surfaces = _Surfaces(pages=pages, decks=decks)
    router = APIRouter()
    router.add_api_route(SOURCES, surfaces.sources_page, methods=["GET"])
    router.add_api_route(NEW, surfaces.add_page, methods=["GET"])
    router.add_api_route(NEW, surfaces.create_source, methods=["POST"])
    router.add_api_route(FETCH, surfaces.fetch_now, methods=["POST"])
    router.add_api_route(CREATED, surfaces.created_page, methods=["GET"])
    return router


def _state_word(state: SourceState, text: LobbyText) -> str:
    """The catalog's own word for what a source is, shown with its shape."""
    return {
        SourceState.REACHABLE: text.source_state_reachable,
        SourceState.ERROR: text.source_state_error,
        SourceState.REFUSED: text.source_state_refused,
        SourceState.NEVER_FETCHED: text.source_state_never_fetched,
    }[state]


def _access_word(kind: AccessKind | None, text: LobbyText) -> str | None:
    """The catalog's tag for how this source is read, when the URL names one."""
    if kind is AccessKind.HTTPS:
        return text.source_access_token
    if kind is AccessKind.SSH:
        return text.source_access_deploy_key
    return None


def _refusal_sentence(reason: SourceRefusal, text: LobbyText) -> str:
    """The one sentence the form shows for that refusal."""
    return {
        SourceRefusal.MALFORMED_NAME: text.source_refused_name,
        SourceRefusal.DUPLICATE_NAME: text.source_refused_duplicate_name,
        SourceRefusal.DUPLICATE_URL: text.source_refused_duplicate_url,
        SourceRefusal.USERINFO: text.source_refused_password,
        SourceRefusal.ACCESS_MISMATCH: text.source_refused_access,
        SourceRefusal.BLANK_ACCESS: text.source_refused_secret,
    }[reason]


def _signed_in(request: Request) -> User:
    """The guard has already turned away everyone else on these addresses."""
    person: User = request.state.signed_in_person
    return person


def _refused() -> Response:
    """Sources is admin only, so a person without the role is turned away."""
    return Response(status_code=HTTPStatus.FORBIDDEN)
