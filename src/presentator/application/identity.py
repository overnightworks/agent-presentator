"""How long a sign-in lasts.

A sign-in lasts at most `SESSION_LIFETIME` from the moment it opened, however
often it is used. A session past its expiry is deleted at the latest at the next
sweep, which comes every `SESSION_SWEEP_INTERVAL`.
"""

from datetime import timedelta
from typing import Final

SESSION_LIFETIME: Final = timedelta(hours=12)
SESSION_SWEEP_INTERVAL: Final = timedelta(minutes=5)
