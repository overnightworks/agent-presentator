"""Names that a production site reaches at a site `vulture` cannot see.

A Jinja template is not Python, so a field only a template renders looks unused.
Every entry names the site that reaches it; an entry without one is an excuse.
"""

from presentator.api.auth import DeckRow
from presentator.contracts.text import LobbyText

# `presentator/api/templates/home.html` renders it in the "Changed" column.
DeckRow.changed

# The words below reach a person through a Jinja template alone. That a catalog
# carries each of them is proven by `load_lobby_text`, which refuses a catalog
# missing any field of `LobbyText`; that a page renders them is proven by the
# route tests.
LobbyText.section_decks
LobbyText.login_title
LobbyText.login_username
LobbyText.login_password
LobbyText.setup_title
LobbyText.setup_once
LobbyText.setup_explanation
LobbyText.setup_username
LobbyText.setup_password
LobbyText.setup_repeat_password
LobbyText.decks_title
LobbyText.decks_column_deck
LobbyText.deck_back
LobbyText.deck_status
LobbyText.deck_presenter_view
LobbyText.deck_projector_view
LobbyText.deck_unknown_explanation
LobbyText.settings_title
LobbyText.settings_instance_name
LobbyText.settings_default_language
LobbyText.settings_language_hint
LobbyText.settings_default_theme
LobbyText.settings_save
LobbyText.account_preferences
LobbyText.account_language
LobbyText.account_theme
