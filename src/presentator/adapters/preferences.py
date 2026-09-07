"""SQLite rows behind the settings and preference ports (ADR 0006)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from presentator.adapters.sqlite import apply_schema, rows
from presentator.contracts.preferences import (
    InstanceSettings,
    PersonPreferences,
    ThemeChoice,
)

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS instance_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    language_tag TEXT NOT NULL,
    theme TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_preferences (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    language_tag TEXT,
    theme TEXT
);
"""
# An instance has one set of defaults, so the table holds one row and the
# schema refuses a second.
_THE_ONLY_ROW: Final = 1


def create_preference_tables(database: Path) -> None:
    """Make the settings and preference schema exist."""
    apply_schema(database, _SCHEMA)


@dataclass(frozen=True, slots=True)
class SqliteInstanceSettingsStore:
    """The instance defaults, as one row."""

    database: Path

    def read(self) -> InstanceSettings | None:
        """Read the defaults an admin saved."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT name, language_tag, theme FROM instance_settings WHERE id = ?",
                (_THE_ONLY_ROW,),
            ).fetchone()
        if row is None:
            return None
        name, language_tag, theme = row
        return InstanceSettings(
            name=name,
            language_tag=language_tag,
            theme=ThemeChoice(theme),
        )

    def write(self, settings: InstanceSettings) -> None:
        """Write the defaults over whatever stood there."""
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO instance_settings (id, name, language_tag, theme)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " name = excluded.name,"
                " language_tag = excluded.language_tag,"
                " theme = excluded.theme",
                (
                    _THE_ONLY_ROW,
                    settings.name,
                    settings.language_tag,
                    settings.theme.value,
                ),
            )


@dataclass(frozen=True, slots=True)
class SqlitePersonPreferencesStore:
    """One row per person who chose something of their own."""

    database: Path

    def read(self, user_id: str) -> PersonPreferences | None:
        """Read that person's overrides."""
        with rows(self.database) as cursor:
            row = cursor.execute(
                "SELECT language_tag, theme FROM person_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        language_tag, theme = row
        return PersonPreferences(
            language_tag=language_tag,
            theme=None if theme is None else ThemeChoice(theme),
        )

    def write(self, user_id: str, preferences: PersonPreferences) -> None:
        """Write that person's overrides over whatever stood there."""
        theme = preferences.theme
        with rows(self.database) as cursor:
            cursor.execute(
                "INSERT INTO person_preferences (user_id, language_tag, theme)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " language_tag = excluded.language_tag,"
                " theme = excluded.theme",
                (
                    user_id,
                    preferences.language_tag,
                    None if theme is None else theme.value,
                ),
            )
