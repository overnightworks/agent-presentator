"""The lobby's words, so no template and no route carries a literal (ADR 0012)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

DEFAULT_LANGUAGE_TAG: Final = "en"


@dataclass(frozen=True, slots=True, kw_only=True)
class LobbyText:
    """One field per message a catalog file has to fill."""

    language_tag: str
    language_name: str
    wordmark: str
    log_out: str
    section_decks: str
    section_settings: str
    menu_account: str
    menu_theme: str
    theme_follow_system: str
    theme_light: str
    theme_dark: str
    role_admin: str
    role_user: str
    login_title: str
    login_username: str
    login_password: str
    login_submit: str
    login_refused: str
    setup_title: str
    setup_once: str
    setup_explanation: str
    setup_username: str
    setup_password: str
    setup_repeat_password: str
    setup_submit: str
    setup_passwords_differ: str
    settings_title: str
    settings_instance_name: str
    settings_default_language: str
    settings_language_hint: str
    settings_default_theme: str
    settings_save: str
    account_title: str
    account_preferences: str
    account_language: str
    account_theme: str
    account_instance_default: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Language:
    """A language a person can choose, named the way its own catalog names it."""

    tag: str
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Catalogs:
    """Every catalog this instance found, keyed by the language it speaks."""

    by_tag: Mapping[str, LobbyText]

    def speaks(self, language_tag: str) -> bool:
        """Say whether a catalog for that language is installed."""
        return language_tag in self.by_tag

    def text(self, language_tag: str) -> LobbyText:
        """The words of that language, or English once its catalog file is gone."""
        return self.by_tag.get(language_tag) or self.by_tag[DEFAULT_LANGUAGE_TAG]

    def languages(self) -> tuple[Language, ...]:
        """Every language that can be chosen, in the order a reader scans them."""
        offered = (
            Language(tag=tag, name=text.language_name)
            for tag, text in self.by_tag.items()
        )
        return tuple(sorted(offered, key=lambda language: language.name))
