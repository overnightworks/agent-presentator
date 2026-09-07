"""Settings · Sources: the list of what this instance reads.

Admin only, refused exactly as Settings · General is. No secret appears here.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from fastapi import APIRouter, Request, Response
from starlette.responses import RedirectResponse

from presentator.api.pages import Pages
from presentator.api.preferences import SETTINGS
from presentator.application.decks import Decks
from presentator.contracts.decks import SourceState
from presentator.contracts.models import User
from presentator.contracts.text import LobbyText

SOURCES: Final = f"{SETTINGS}/sources"
FETCH: Final = f"{SOURCES}/{{name}}/fetch"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRow:
    """One source as the list renders it: its state, name, URL, and fetched age."""

    name: str
    url: str
    state: str
    state_word: str
    fetched: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Surfaces:
    """The Sources page and the Fetch now that refreshes one row of it."""

    pages: Pages
    decks: Decks

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

    def fetch_now(self, request: Request, name: str) -> Response:
        """Refresh that one source and return to the list."""
        if not _signed_in(request).is_admin:
            return _refused()
        if not self.decks.refresh_named(name):
            return Response(status_code=HTTPStatus.NOT_FOUND)
        return RedirectResponse(SOURCES, status_code=HTTPStatus.SEE_OTHER)

    def _rows(self, text: LobbyText) -> tuple[SourceRow, ...]:
        return tuple(
            SourceRow(
                name=source.name,
                url=source.url,
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
    router.add_api_route(FETCH, surfaces.fetch_now, methods=["POST"])
    return router


def _state_word(state: SourceState, text: LobbyText) -> str:
    """The catalog's own word for what a source is, shown with its shape."""
    return {
        SourceState.REACHABLE: text.source_state_reachable,
        SourceState.ERROR: text.source_state_error,
        SourceState.NEVER_FETCHED: text.source_state_never_fetched,
    }[state]


def _signed_in(request: Request) -> User:
    """The guard has already turned away everyone else on these addresses."""
    person: User = request.state.signed_in_person
    return person


def _refused() -> Response:
    """Sources is admin only, so a person without the role is turned away."""
    return Response(status_code=HTTPStatus.FORBIDDEN)
