"""Names that a production site reaches at a site `vulture` cannot see.

A Jinja template is not Python, so a field only a template renders looks unused.
Every entry names the site that reaches it; an entry without one is an excuse.
"""

from presentator.api.auth import DeckRow

# `presentator/api/templates/home.html` renders it in the "Changed" column.
DeckRow.changed
