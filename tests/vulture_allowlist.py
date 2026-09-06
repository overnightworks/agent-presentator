"""Names that a production site reaches at a site `vulture` cannot see.

Protocol methods have no runtime body; adapters in later slices implement them.
"""

from presentator.ports.catalog import BuildRunner, DeckStore, SourceStore
from presentator.ports.identity import (
    Clock,
    LoginAttemptStore,
    PasswordHasher,
    SessionRecordStore,
    UserStore,
)


class _ProtocolArgumentNames:
    """Argument names bound by Protocol signatures, unread until an adapter exists."""

    password = ""
    password_hash = ""
    since = ""


_ = (
    UserStore.get,
    UserStore.get_by_username,
    UserStore.put,
    UserStore.count,
    SessionRecordStore.get,
    SessionRecordStore.put,
    SessionRecordStore.remove,
    LoginAttemptStore.record_failure,
    LoginAttemptStore.failure_count,
    PasswordHasher.hash,
    PasswordHasher.verify,
    Clock.now,
    DeckStore.get,
    DeckStore.put,
    DeckStore.list_newest_first,
    DeckStore.remove,
    SourceStore.get,
    SourceStore.put,
    SourceStore.list,
    BuildRunner.run,
    _ProtocolArgumentNames.password,
    _ProtocolArgumentNames.password_hash,
    _ProtocolArgumentNames.since,
)
