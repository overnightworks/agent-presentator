"""Names that a production site reaches at a site `vulture` cannot see.

One entry exists only together with the site that reaches it.
"""

from presentator.contracts.text import LobbyText

# Every word below is read by a Jinja template alone (`{{ text.login_title }}`),
# and `vulture` parses Python, not templates. That a catalog carries each of
# them is proven by `load_lobby_text`, which refuses a catalog missing any field
# of `LobbyText`; that the pages render them is proven by the route tests.
_ = (
    LobbyText.section_decks,
    LobbyText.login_title,
    LobbyText.login_username,
    LobbyText.login_password,
    LobbyText.setup_title,
    LobbyText.setup_once,
    LobbyText.setup_explanation,
    LobbyText.setup_username,
    LobbyText.setup_password,
    LobbyText.setup_repeat_password,
    LobbyText.settings_title,
    LobbyText.settings_instance_name,
    LobbyText.settings_default_language,
    LobbyText.settings_language_hint,
    LobbyText.settings_default_theme,
    LobbyText.settings_save,
    LobbyText.account_preferences,
    LobbyText.account_language,
    LobbyText.account_theme,
)
