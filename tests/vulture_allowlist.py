"""Names that a production site reaches at a site `vulture` cannot see.

A Jinja template is not Python, so a field only a template renders looks unused.
Every entry names the site that reaches it; an entry without one is an excuse.
"""

from presentator.api.auth import DeckRow
from presentator.api.decks import Banner
from presentator.api.sources import SourceRow
from presentator.contracts.text import LobbyText
from presentator.host.config import Settings

# `presentator/api/templates/home.html` renders it in the "Changed" column.
DeckRow.changed

# `presentator/api/templates/sources.html` renders each row of the Sources list.
SourceRow.name
SourceRow.url
SourceRow.access
SourceRow.state
SourceRow.state_word
SourceRow.fetched

# `presentator/api/templates/deck.html` renders the build block out of these.
Banner.lead
Banner.when
Banner.sentences

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
LobbyText.sources_back
LobbyText.source_access
LobbyText.source_secret
LobbyText.source_secret_info_mark
LobbyText.source_secret_info
LobbyText.source_webhook_later
LobbyText.source_webhook
LobbyText.source_webhook_address
LobbyText.source_webhook_secret
LobbyText.source_copy
LobbyText.source_webhook_payload
LobbyText.source_created_done
LobbyText.source_created

# Still loaded from the environment; the hook compares the per-source hash.
Settings.source_hook_secret
