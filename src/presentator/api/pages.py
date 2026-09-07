"""The one place a lobby template is rendered, with the words every page shows.

A page that stands behind the session also carries the header the picture
gives every signed-in surface, so no route builds that context itself.
"""

from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates

from presentator.contracts.text import LobbyText

if TYPE_CHECKING:
    from presentator.contracts.models import User

_TEMPLATES: Final = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass(frozen=True, slots=True, kw_only=True)
class PageRenderer:
    """Turns a template and its words into the answer a browser reads."""

    text: LobbyText

    def page(
        self,
        request: Request,
        name: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        **words: object,
    ) -> Response:
        """Render the template with the words the whole lobby carries."""
        return _TEMPLATES.TemplateResponse(
            request,
            name,
            {
                "language": self.text.language_tag,
                "wordmark": self.text.wordmark,
                **words,
            },
            status_code=status,
        )

    def signed_in_page(
        self,
        request: Request,
        name: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        **words: object,
    ) -> Response:
        """Render a page behind the session, with the header it wears."""
        person: User = request.state.signed_in_person
        return self.page(
            request,
            name,
            status=status,
            person=person.username,
            log_out=self.text.log_out,
            section_decks=self.text.section_decks,
            **words,
        )
