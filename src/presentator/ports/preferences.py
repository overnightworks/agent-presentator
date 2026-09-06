"""The stores Settings and Account read and write; adapters fill them."""

from abc import abstractmethod
from typing import Protocol

from presentator.contracts.preferences import InstanceSettings, PersonPreferences


class InstanceSettingsStore(Protocol):
    """The one row an admin edits under Settings."""

    @abstractmethod
    def read(self) -> InstanceSettings | None:
        """Return what an admin saved, or nothing while nobody has."""

    @abstractmethod
    def write(self, settings: InstanceSettings) -> None:
        """Keep the instance defaults, replacing whatever stood there."""


class PersonPreferencesStore(Protocol):
    """The overrides that belong beside one account."""

    @abstractmethod
    def read(self, user_id: str) -> PersonPreferences | None:
        """Return that person's overrides, or nothing while they chose none."""

    @abstractmethod
    def write(self, user_id: str, preferences: PersonPreferences) -> None:
        """Keep that person's overrides, replacing whatever stood there."""
