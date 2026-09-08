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
    decks_title: str
    decks_column_state: str
    decks_column_deck: str
    decks_column_changed: str
    decks_empty_title: str
    decks_empty_explanation: str
    deck_back: str
    deck_status: str
    deck_state_ready: str
    deck_state_building: str
    deck_state_failed: str
    deck_state_never_built: str
    deck_built: str
    deck_not_built_explanation: str
    deck_building_explanation: str
    deck_building_label: str
    deck_building_for: str
    deck_failed_title: str
    deck_attempt_label: str
    deck_failed_without_a_message: str
    deck_failed_last_talk_stands: str
    deck_presenter_view: str
    deck_projector_view: str
    deck_pdf: str
    deck_unknown_title: str
    deck_unknown_explanation: str
    settings_title: str
    settings_sources: str
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
    sources_column_source: str
    sources_column_fetched: str
    sources_fetch_now: str
    source_state_reachable: str
    source_state_error: str
    source_state_refused: str
    source_state_never_fetched: str
    sources_empty_title: str
    sources_empty_explanation: str
    sources_empty_need: str
    sources_empty_ssh: str
    sources_empty_or: str
    sources_empty_https: str
    sources_add: str
    sources_back: str
    sources_column_access: str
    source_name: str
    source_url: str
    source_access: str
    source_access_ssh: str
    source_access_https: str
    source_access_token: str
    source_access_deploy_key: str
    source_secret: str
    source_secret_info_mark: str
    source_secret_info: str
    source_webhook_later: str
    source_check: str
    source_create: str
    source_created: str
    source_webhook: str
    source_webhook_address: str
    source_webhook_secret: str
    source_webhook_once: str
    source_webhook_held_elsewhere: str
    source_copy: str
    source_webhook_payload: str
    source_created_done: str
    source_created_toast: str
    source_refused_name: str
    source_refused_duplicate_name: str
    source_refused_duplicate_url: str
    source_refused_password: str
    source_refused_access: str
    source_refused_secret: str
    source_access_heading: str
    source_webhook_heading: str
    source_recent_runs: str
    source_decks: str
    source_reverse: str
    source_push_inbox: str
    source_later: str
    source_renew: str
    source_secret_dots: str
    source_secret_missing: str
    source_access_renew_info: str
    source_run_fetched: str
    source_run_unreachable: str
    source_run_secret: str
    source_run_refused: str
    source_run_column_state: str
    source_run_column_fetched: str
    source_run_column_detail: str


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
