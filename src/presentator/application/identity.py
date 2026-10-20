"""How long a sign-in lasts.

A sign-in lasts at most `SESSION_LIFETIME` from the moment it opened, however
often it is used.
"""

from datetime import timedelta
from typing import Final

SESSION_LIFETIME: Final = timedelta(hours=12)
