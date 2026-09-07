"""The instance's defaults, a person's overrides, and how a page resolves them.

The order is the operator's ruling of 06.09.2026 on issue #8, line 22: the
person's Account preference first, the instance default behind it.
"""

from dataclasses import dataclass

from presentator.contracts.preferences import (
    Appearance,
    InstanceSettings,
    PersonPreferences,
)
from presentator.contracts.text import Catalogs, Language
from presentator.ports.preferences import (
    InstanceSettingsStore,
    PersonPreferencesStore,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Preferences:
    """Every use case the lobby has around language and theme."""

    instance: InstanceSettingsStore
    people: PersonPreferencesStore
    catalogs: Catalogs

    def instance_settings(self) -> InstanceSettings:
        """What an admin set, or what a fresh instance starts with."""
        return self.instance.read() or InstanceSettings()

    def save_instance_settings(self, settings: InstanceSettings) -> None:
        """Keep the defaults every person without an override reads."""
        self.instance.write(settings)

    def preferences_of(self, user_id: str) -> PersonPreferences:
        """What that person chose, with an unset field meaning the instance."""
        return self.people.read(user_id) or PersonPreferences()

    def save_preferences_of(self, user_id: str, chosen: PersonPreferences) -> None:
        """Keep that person's overrides; the instance default stays untouched."""
        self.people.write(user_id, chosen)

    def appearance_for(self, user_id: str | None) -> Appearance:
        """The words and the look, the person's choice first, the instance's next."""
        settings = self.instance_settings()
        chosen = (
            PersonPreferences() if user_id is None else self.preferences_of(user_id)
        )
        return Appearance(
            text=self.catalogs.text(chosen.language_tag or settings.language_tag),
            theme=chosen.theme or settings.theme,
        )

    def languages(self) -> tuple[Language, ...]:
        """Every language a select may offer."""
        return self.catalogs.languages()

    def speaks(self, language_tag: str) -> bool:
        """Say whether this instance has a catalog for a submitted language."""
        return self.catalogs.speaks(language_tag)
