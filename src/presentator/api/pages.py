"""How a lobby page is rendered: its words, its look, and the chrome it carries.

Every page resolves its own language and theme, because both are a person's
choice (ADR 0012) rather than a property of the instance the routes were built
with.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates

from presentator.application.preferences import Preferences
from presentator.contracts.models import User
from presentator.contracts.preferences import Appearance, ThemeChoice
from presentator.contracts.text import LobbyText

_TEMPLATES: Final = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass(frozen=True, slots=True, kw_only=True)
class Option:
    """One row of a select or of the menu's radio group."""

    value: str
    label: str
    chosen: bool


def options(choices: Iterable[tuple[str, str]], *, chosen: str) -> tuple[Option, ...]:
    """Mark the choice that is the current one and leave the order alone."""
    return tuple(
        Option(value=value, label=label, chosen=value == chosen)
        for value, label in choices
    )


def theme_choices(text: LobbyText) -> tuple[tuple[str, str], ...]:
    """The three looks, in the order the picture's theme rows show them."""
    return (
        (ThemeChoice.FOLLOW_SYSTEM.value, text.theme_follow_system),
        (ThemeChoice.LIGHT.value, text.theme_light),
        (ThemeChoice.DARK.value, text.theme_dark),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Pages:
    """Renders every lobby page in the words and the look its reader chose."""

    preferences: Preferences

    def appearance(self, request: Request) -> Appearance:
        """The words and the look the person behind this request reads in."""
        person = signed_in_person(request)
        return self.preferences.appearance_for(None if person is None else person.id)

    def page(self, request: Request, name: str, **content: object) -> Response:
        """Render a page, with the header a signed-in person sees on every one."""
        appearance = self.appearance(request)
        return _TEMPLATES.TemplateResponse(
            request,
            name,
            {
                "text": appearance.text,
                "theme": appearance.explicit_theme,
                "person": signed_in_person(request),
                "theme_rows": options(
                    theme_choices(appearance.text),
                    chosen=appearance.theme,
                ),
                **content,
            },
        )


def signed_in_person(request: Request) -> User | None:
    """Who the guard let through, or nobody on the pages that need no session."""
    person: User | None = request.state.signed_in_person
    return person
