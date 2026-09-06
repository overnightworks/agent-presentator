"""The lobby's words, so no template and no route carries a literal (ADR 0012)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class LobbyText:
    """One field per message a catalog file has to fill."""

    language_tag: str
    wordmark: str
    log_out: str
    section_decks: str
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
    decks_column_deck: str
    decks_column_changed: str
    decks_empty_title: str
    decks_empty_explanation: str
