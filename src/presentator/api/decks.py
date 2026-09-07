"""The page one deck stands on, and the built talk delivered from that page.

The address names a deck; the directory that deck's row points at is what the
talk is served from. Nothing a request carries ever becomes part of a path:
Starlette's static files resolve every asset below that directory and refuse
whatever would leave it, and the session guard in front of the whole lobby
covers every address here, the presenter and projector views included.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from presentator.api.pages import PageRenderer
from presentator.application.decks import Decks
from presentator.contracts.text import LobbyText

_DECK_PAGE: Final = "/deck/{slug}"
_DECK_TEMPLATE: Final = "deck.html"
_UNKNOWN_DECK_TEMPLATE: Final = "deck_unknown.html"


@dataclass(frozen=True, slots=True, kw_only=True)
class _DeckPage:
    """The page that says what a deck is and offers the ways into it."""

    decks: Decks
    renderer: PageRenderer
    text: LobbyText

    def deck(self, request: Request, slug: str) -> Response:
        """Show the deck's title, where it came from, and its two views."""
        page = self.decks.page(slug)
        if page is None:
            return self.renderer.signed_in_page(
                request,
                _UNKNOWN_DECK_TEMPLATE,
                status=HTTPStatus.NOT_FOUND,
                back=self.text.deck_back,
                title=self.text.deck_unknown_title,
                explanation=self.text.deck_unknown_explanation,
            )
        return self.renderer.signed_in_page(
            request,
            _DECK_TEMPLATE,
            back=self.text.deck_back,
            title=page.title,
            slug=page.slug,
            built=page.built,
            state=(
                self.text.deck_state_ready
                if page.built
                else self.text.deck_state_not_built
            ),
            status_label=self.text.deck_status,
            source=page.source,
            commit=page.commit,
            presenter_view=self.text.deck_presenter_view,
            projector_view=self.text.deck_projector_view,
            not_built=self.text.deck_not_built_explanation,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _BuiltTalk:
    """Delivers the deck's built talk: its projector view, presenter view, assets."""

    decks: Decks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the asset below the deck's build directory that the path names."""
        directory = self.decks.built_talk(scope["path_params"]["slug"])
        if directory is None:
            await Response(status_code=HTTPStatus.NOT_FOUND)(scope, receive, send)
            return
        # Which directory a deck delivers from is a row, not a setting, so the
        # files are rooted per request rather than once at startup.
        await StaticFiles(directory=directory, html=True)(scope, receive, send)


def add_deck_pages(
    lobby: FastAPI,
    *,
    decks: Decks,
    renderer: PageRenderer,
    text: LobbyText,
) -> None:
    """Give the lobby the deck page, and the talk that stands under it."""
    page = _DeckPage(decks=decks, renderer=renderer, text=text)
    # The page answers its own address; everything below it is the talk, so the
    # route is registered first and the mount catches the rest.
    lobby.add_api_route(_DECK_PAGE, page.deck, methods=["GET"])
    lobby.mount(_DECK_PAGE, _BuiltTalk(decks=decks), name="talk")
