"""Names that something other than a Python call site reaches.

A Jinja template is not Python, so a field only a template renders looks
unused; a standard-library base class's own dispatch is invisible the same
way. Every entry names the site that reaches it; an entry without one is an
excuse.
"""

from presentator.api.auth import DeckRow
from presentator.api.decks import Banner
from presentator.api.sources import (
    SourceDeckRow,
    SourceRemovalView,
    SourceRow,
    SourceRunRow,
    SourceView,
)
from presentator.contracts.text import LobbyText
from presentator.contracts.voice import VoiceLoadOutcome, VoiceState
from tests.gitmirror.test_mirror import DumbHttpHandler, RespondingHandler

# `BaseHTTPRequestHandler`'s own request dispatch calls this by name; nothing
# in the test that stands the server up ever calls it itself.
DumbHttpHandler.log_message
RespondingHandler.log_message

# `presentator/api/templates/home.html` renders it in the "Changed" column.
DeckRow.changed

# `presentator/api/templates/sources.html` renders each row of the Sources list.
SourceRow.name
SourceRow.url
SourceRow.access
SourceRow.state
SourceRow.state_word
SourceRow.fetched

# `presentator/api/templates/source.html` renders the source page.
SourceView.name
SourceView.url
SourceView.access
SourceView.state
SourceView.state_word
SourceView.fetched
SourceView.secret_missing
SourceView.address
SourceView.webhook_secret
SourceView.webhook_secret_held_elsewhere
SourceView.carries_no_secret
SourceView.renews_deploy_key
SourceView.public_key
SourceView.runs
SourceView.decks
SourceRunRow.state
SourceRunRow.state_word
SourceRunRow.fetched
SourceRunRow.commit
SourceRunRow.reason
SourceDeckRow.slug
SourceDeckRow.title

# `presentator/api/templates/source_remove.html` renders the confirm page.
SourceRemovalView.name
SourceRemovalView.title
SourceRemovalView.body

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
LobbyText.source_deploy_key_hint
LobbyText.source_webhook
LobbyText.source_webhook_address
LobbyText.source_webhook_secret
LobbyText.source_webhook_held_elsewhere
LobbyText.source_copy
LobbyText.source_webhook_payload
LobbyText.source_created_done
LobbyText.source_created
LobbyText.source_access_heading
LobbyText.source_webhook_heading
LobbyText.source_recent_runs
LobbyText.source_decks
LobbyText.source_reverse
LobbyText.source_push_inbox
LobbyText.source_later
LobbyText.source_renew
LobbyText.source_secret_dots
LobbyText.source_secret_missing
LobbyText.source_access_renew_info
LobbyText.source_deploy_key_info
LobbyText.source_public_key
LobbyText.source_deploy_key_renew
LobbyText.source_run_fetched
LobbyText.source_run_unreachable
LobbyText.source_run_secret
LobbyText.source_remove_heading
LobbyText.source_remove_hint
LobbyText.source_remove_button
LobbyText.source_remove_cancel
LobbyText.source_remove_confirm

# `presentator/api/templates/voice.html` renders these Voice-tab words.
LobbyText.settings_voice
LobbyText.voice_model
LobbyText.voice_language
LobbyText.voice_state
LobbyText.voice_state_active
LobbyText.voice_state_loading
LobbyText.voice_state_failed
LobbyText.voice_state_downloaded
LobbyText.voice_state_not_downloaded
LobbyText.voice_state_unavailable
LobbyText.voice_unknown_title
LobbyText.voice_unknown_explanation
LobbyText.voice_check_again
LobbyText.voice_load
LobbyText.voice_recovery_failed
LobbyText.voice_recovery_invalid_selection
LobbyText.voice_recovery_durability

# Pydantic constructs these private-wire values; `voice.html` renders states by value.
VoiceState.LOADING
VoiceState.DOWNLOADED
VoiceState.NOT_DOWNLOADED
VoiceLoadOutcome.NOT_ACTIVATED
