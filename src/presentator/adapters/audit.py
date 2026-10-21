"""Where the account events of a sign-in and a back-channel logout are written."""

import logging

from webauth.ports import AccountEvent

_log = logging.getLogger(__name__)


class LoggingAuditSink:
    """Account events as log lines: the kind, the subject and the roles.

    The event carries no username and no token, so neither can reach the log.
    """

    def account_event(self, event: AccountEvent) -> None:
        """Write one line per event at INFO."""
        _log.info(
            "account event %s: subject %s, role %s, previous role %s",
            event.kind.value,
            event.subject_id,
            event.role,
            event.previous_role,
        )
