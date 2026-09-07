"""The page one deck stands on, the talk it delivers, and its PDF download.

The address names a deck; what that deck's row points at is what the talk is
served from and what the download hands over. Nothing a request carries ever
becomes part of a path: Starlette's static files resolve every asset below the
build directory and refuse whatever would leave it, a path that is not a file
is the application's `index.html`, the export is the file the row names, and
the session guard in front of the whole lobby covers every address here, the
two views and the download included.
"""

from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from presentator.api.pages import Pages, state_word
from presentator.application.decks import Decks
from presentator.contracts.decks import DECK_PATH, DeckPage, DeckState, ShownAttempt
from presentator.contracts.text import LobbyText

_DECK_PAGE: Final = f"{DECK_PATH}/{{slug}}"
_DECK_PDF: Final = f"{_DECK_PAGE}/pdf"
_DECK_TEMPLATE: Final = "deck.html"
_UNKNOWN_DECK_TEMPLATE: Final = "deck_unknown.html"
_PDF_TYPE: Final = "application/pdf"
_SAVED_AS: Final = "{slug}.pdf"
_APPLICATION: Final = "index.html"


@dataclass(frozen=True, slots=True, kw_only=True)
class Banner:
    """What a deck page says about the build that ran last, above the ways in.

    Every word of it is already in the reader's language: the template places
    them, it does not choose them.
    """

    lead: str
    label: str
    commit: str
    when: str
    failure: str | None
    sentences: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class _DeckPage:
    """The page that says what a deck is and offers the ways into it."""

    decks: Decks
    pages: Pages

    def deck(self, request: Request, slug: str) -> Response:
        """Show the deck's title, its state, where it came from, and the ways in."""
        page = self.decks.page(slug)
        if page is None:
            return self._no_such_deck(request)
        text = self.pages.appearance(request).text
        return self.pages.page(
            request,
            _DECK_TEMPLATE,
            title=page.title,
            slug=page.slug,
            built=self._when_it_was_built(page.built_ago, text),
            source=page.source,
            commit=page.commit,
            state=page.state.value,
            state_word=state_word(page.state, text),
            # A build that is running may deliver a half-written talk at any
            # moment, so the two views are not offered while one runs; the PDF
            # beside them is the last good build's file and stays offered.
            views_open=page.state is not DeckState.BUILDING,
            banner=self._banner(page, text),
        )

    def _banner(self, page: DeckPage, text: LobbyText) -> Banner | None:
        """The building or the failure block, while there is one to show."""
        attempt = page.attempt
        if attempt is None:
            return None
        if page.state is DeckState.BUILDING:
            return self._it_is_building(attempt, text)
        return self._it_failed(page, attempt, text)

    def _it_is_building(self, attempt: ShownAttempt, text: LobbyText) -> Banner:
        """That a build is running, on which commit, and for how long (line 10)."""
        running_for = self.pages.duration_in_words(attempt.ago, text.language_tag)
        return Banner(
            lead=text.deck_building_explanation,
            label=text.deck_building_label,
            commit=attempt.commit,
            when=text.deck_building_for.format(age=running_for),
            failure=None,
            sentences=(),
        )

    def _it_failed(
        self,
        page: DeckPage,
        attempt: ShownAttempt,
        text: LobbyText,
    ) -> Banner:
        """What broke, when it was tried, and what still stands (lines 9, 16)."""
        no_words = () if attempt.failure else (text.deck_failed_without_a_message,)
        still_standing = (
            (text.deck_failed_last_talk_stands,) if page.built_ago is not None else ()
        )
        return Banner(
            lead=text.deck_failed_title,
            label=text.deck_attempt_label,
            commit=attempt.commit,
            when=self.pages.age_in_words(attempt.ago, text.language_tag),
            failure=attempt.failure,
            sentences=(*no_words, *still_standing),
        )

    def pdf(self, request: Request, slug: str) -> Response:
        """Hand over the file this deck's row names, saved under the deck's name.

        The address names the deck, never the file: the path comes from the row
        the slug is looked up in, and a slug that could not be a file's name has
        already stopped that lookup.
        """
        export = self.decks.exported_pdf(slug)
        if export is None or not export.is_file():
            return self._no_such_deck(request)
        return FileResponse(
            export,
            media_type=_PDF_TYPE,
            filename=_SAVED_AS.format(slug=slug),
        )

    def _no_such_deck(self, request: Request) -> Response:
        """The lobby's own page for an address no deck stands under."""
        return self.pages.page(
            request,
            _UNKNOWN_DECK_TEMPLATE,
            status=HTTPStatus.NOT_FOUND,
        )

    def _when_it_was_built(
        self,
        built_ago: timedelta | None,
        text: LobbyText,
    ) -> str | None:
        """How long ago the delivered talk was built, once one is delivered.

        The one value says both that a talk stands and how old it is, so the
        page cannot offer a view of a build whose time it cannot name.
        """
        if built_ago is None:
            return None
        age = self.pages.age_in_words(built_ago, text.language_tag)
        return text.deck_built.format(age=age)


@dataclass(frozen=True, slots=True, kw_only=True)
class _BuiltTalk:
    """Delivers the deck's built talk: its projector view, presenter view, assets."""

    decks: Decks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the asset below the deck's build directory that the path names.

        A Slidev build is one single-page application: the presenter is a
        client-side route of the same `index.html` as the projector, not a
        second file. A path that is not a file under the talk is that
        application, so a wrong address under a deck is not an inventory of
        what the build wrote.
        """
        directory = self.decks.built_talk(scope["path_params"]["slug"])
        if directory is None:
            await Response(status_code=HTTPStatus.NOT_FOUND)(scope, receive, send)
            return
        # Which directory a deck delivers from is a row, not a setting, so the
        # files are rooted per request rather than once at startup.
        await _TalkFiles(directory)(scope, receive, send)


class _TalkFiles(StaticFiles):
    """A Slidev single-page application: its files, then its `index.html`."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True)
        self._root = directory

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve a real file, or the application for any path still inside the talk."""
        if _leaves_the_talk(self._root, path):
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as missing:
            if missing.status_code != HTTPStatus.NOT_FOUND:
                raise
            return self._the_application()
        if response.status_code != HTTPStatus.NOT_FOUND:
            return response
        return self._the_application()

    def _the_application(self) -> Response:
        """The built talk's own `index.html`, or nothing if that file is gone."""
        index = self._root / _APPLICATION
        if not index.is_file():
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND)
        return FileResponse(index)


def _leaves_the_talk(directory: Path, path: str) -> bool:
    """Whether resolving this path would step outside the talk's directory."""
    root = directory.resolve()
    return not (directory / path).resolve().is_relative_to(root)


def add_deck_pages(lobby: FastAPI, *, decks: Decks, pages: Pages) -> None:
    """Give the lobby the deck page, its download, and the talk under them."""
    page = _DeckPage(decks=decks, pages=pages)
    # The page and the download answer their own addresses; everything else
    # below the deck is the talk, so the routes are registered first and the
    # mount catches the rest.
    lobby.add_api_route(_DECK_PAGE, page.deck, methods=["GET"])
    lobby.add_api_route(_DECK_PDF, page.pdf, methods=["GET"])
    lobby.mount(_DECK_PAGE, _BuiltTalk(decks=decks), name="talk")
