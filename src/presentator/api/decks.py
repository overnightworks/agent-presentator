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

from presentator.api.pages import Pages
from presentator.application.decks import Decks

_DECK_PAGE: Final = "/deck/{slug}"
_DECK_TEMPLATE: Final = "deck.html"
_UNKNOWN_DECK_TEMPLATE: Final = "deck_unknown.html"


@dataclass(frozen=True, slots=True, kw_only=True)
class _DeckPage:
    """The page that says what a deck is and offers the ways into it."""

    decks: Decks
    pages: Pages

    def deck(self, request: Request, slug: str) -> Response:
        """Show the deck's title, where it came from, and its two views."""
        page = self.decks.page(slug)
        if page is None:
            return self.pages.page(
                request,
                _UNKNOWN_DECK_TEMPLATE,
                status=HTTPStatus.NOT_FOUND,
            )
        return self.pages.page(
            request,
            _DECK_TEMPLATE,
            title=page.title,
            slug=page.slug,
            built=page.built,
            source=page.source,
            commit=page.commit,
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


def add_deck_pages(lobby: FastAPI, *, decks: Decks, pages: Pages) -> None:
    """Give the lobby the deck page, and the talk that stands under it."""
    page = _DeckPage(decks=decks, pages=pages)
    # The page answers its own address; everything below it is the talk, so the
    # route is registered first and the mount catches the rest.
    lobby.add_api_route(_DECK_PAGE, page.deck, methods=["GET"])
    lobby.mount(_DECK_PAGE, _BuiltTalk(decks=decks), name="talk")
