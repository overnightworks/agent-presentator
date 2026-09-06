"""Names that a production site reaches at a site `vulture` cannot see.

`presentator.contracts.models` is frozen ahead of its callers (AGENTS.md, "Code
built ahead of its caller"): slice 2 (#23) reads `User`; the catalog slice
(#8, "One git deck listed", not yet its own item) reads `Deck` and `Source`.
Vulture cannot see a caller that does not exist yet, so each field a slice will
read is named here until that slice lands and reads it for real.
"""

from datetime import UTC, datetime

from presentator.contracts.models import Deck, Role, Source, User

_user = User(id="", username="", role=Role.ADMIN)
_deck = Deck(
    slug="",
    title="",
    owner_id="",
    source_id="",
    changed_at=datetime.fromtimestamp(0, tz=UTC),
    active_build_path="",
    last_error="",
)
_source = Source(id="", owner_id="", url="", ref="", credential_ref="")

_ = (
    _user.username,
    _user.role,
    _deck.owner_id,
    _deck.source_id,
    _deck.changed_at,
    _deck.active_build_path,
    _deck.last_error,
    _source.owner_id,
    _source.url,
    _source.ref,
    _source.credential_ref,
)
