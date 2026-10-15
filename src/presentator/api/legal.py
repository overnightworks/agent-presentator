"""The Datenschutzerklärung: the one owner of its words, rendered by its own page.

It is German whatever language a reader chose, because it is the operator's
legal declaration, not interface wording a catalog translates (ADR 0012). The
Impressum is not here: every page links the operator's one Impressum instead of
carrying a copy (ADR 0015).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from presentator.application.identity import FAILURES_BEFORE_THROTTLE, IDLE_WINDOW

_SECONDS_PER_HOUR: Final = 3600


class Emphasis(StrEnum):
    """How a phrase stands out from the sentence around it."""

    PLAIN = "plain"
    LABEL = "label"
    OPEN_POINT = "open_point"
    IMPRESSUM = "impressum"


@dataclass(frozen=True, slots=True)
class Phrase:
    """A run of words that all read the same way."""

    words: str
    emphasis: Emphasis = Emphasis.PLAIN


type Paragraph = tuple[Phrase, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class LegalSection:
    """One numbered heading and what it says, as prose or as listed points."""

    heading: str
    paragraphs: tuple[Paragraph, ...]
    listed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class LegalDocument:
    """A legal text in the one language it is published in."""

    language_tag: str
    title: str
    introduction: Paragraph
    sections: tuple[LegalSection, ...]


def _said(*parts: str | Phrase) -> Paragraph:
    return tuple(part if isinstance(part, Phrase) else Phrase(part) for part in parts)


def _point(label: str, *rest: str | Phrase) -> Paragraph:
    return _said(Phrase(label, Emphasis.LABEL), " ", *rest)


def _open(words: str) -> Phrase:
    return Phrase(f"[offen: {words}]", Emphasis.OPEN_POINT)


_IMPRESSUM: Final = Phrase("Impressum", Emphasis.IMPRESSUM)
_IDLE_HOURS: Final = int(IDLE_WINDOW.total_seconds()) // _SECONDS_PER_HOUR

DATENSCHUTZ: Final = LegalDocument(
    language_tag="de",
    title="Datenschutzerklärung",
    introduction=_said(
        "Entwurf, aus dem Code abgeleitet. Jede Stelle, die sich nicht aus dem "
        "Code ergibt, ist mit ",
        _open("…"),
        " markiert statt geraten.",
    ),
    sections=(
        LegalSection(
            heading="1. Verantwortlicher",
            paragraphs=(
                _said(
                    "Verantwortlich für diese Anwendung ist der Betreiber. "
                    "Kontaktdaten: siehe ",
                    _IMPRESSUM,
                    ".",
                ),
            ),
        ),
        LegalSection(
            heading="2. Welche Daten wir verarbeiten",
            listed=True,
            paragraphs=(
                _point(
                    "Benutzerkonto:",
                    "Benutzername, Rolle (Admin/Nutzer) und ein mit Argon2id "
                    "gehashtes Passwort. Konten legt ausschließlich ein "
                    "Administrator an; es gibt keine Selbstregistrierung.",
                ),
                _point(
                    "Sitzungen und Cookies:",
                    "ein serverseitig signiertes Sitzungs-Cookie (HttpOnly, "
                    "SameSite=Lax, nur über HTTPS) sowie ein zweites, von "
                    "JavaScript lesbares Cookie ausschließlich zum "
                    "Fälschungsschutz (CSRF, kein Tracking). Gespeichert werden "
                    "Sitzungs-ID, Benutzer-ID, IP-Adresse, User-Agent und der "
                    "Zeitpunkt der letzten Aktivität. Eine Sitzung gilt nach "
                    f"{_IDLE_HOURS} Stunden Inaktivität als beendet; die Zeile "
                    "wird beim Abmelden gelöscht.",
                ),
                _point(
                    "Fehlgeschlagene Anmeldeversuche:",
                    "Benutzername, IP-Adresse und Zeitpunkt, zum Schutz vor "
                    "automatisiertem Ausprobieren (nach "
                    f"{FAILURES_BEFORE_THROTTLE} Fehlversuchen wird gedrosselt).",
                ),
                _point(
                    "Decks und Quellen:",
                    "der Inhalt Ihrer verbundenen Git-Quellen (Folientitel, "
                    "Folieninhalt, Sprechernotizen) wird auf den Server "
                    "gespiegelt und zu Vorträgen gebaut. Ein "
                    "Lesezugriffsschlüssel pro Quelle wird verschlüsselt "
                    "gespeichert und kann nach der Eingabe nur erneuert, nie "
                    "wieder angezeigt werden.",
                ),
                _point(
                    "CoPresenter (Sprachfunktion):",
                    "Ihre gesprochene Frage wird lokal in Text umgewandelt; die "
                    "Audio-Rohdaten werden dabei nicht gespeichert. Der erkannte "
                    "Text, die aktuelle Folie und der vollständige Deckinhalt "
                    "werden an den in Abschnitt 3 genannten KI-Anbieter "
                    "gesendet. Die gesprochene Antwort wird lokal erzeugt und "
                    "nur für die Wiedergabe gestreamt, nicht dauerhaft "
                    "gespeichert.",
                ),
                _point(
                    "Server-Protokolle:",
                    "die Anwendung schreibt Betriebsprotokolle. ",
                    _open(
                        "Aufbewahrungsdauer und Speicherort liegen an der "
                        "Server-Konfiguration des Betreibers, nicht im "
                        "Anwendungscode.",
                    ),
                ),
            ),
        ),
        LegalSection(
            heading="3. Empfänger und externe Anbieter",
            listed=True,
            paragraphs=(
                _point(
                    "Anthropic (Claude):",
                    "aktuell der einzige aktiv genutzte Antwort-Anbieter des "
                    "CoPresenter-Features, über den eigenen Claude-Zugang des "
                    "Betreibers, nicht über einen pro Anfrage übertragenen "
                    "API-Schlüssel. Übertragen werden die gesprochene Frage als "
                    "Text, die aktuelle Folie und der gesamte Deckinhalt. ",
                    _open(
                        "Anthropics eigene Aufbewahrungs- und "
                        "Verarbeitungspraxis liegt außerhalb dieses Codes.",
                    ),
                ),
                _point(
                    "Grok, Codex:",
                    "die Technik ist vorbereitet, wird vom Code aktuell aber "
                    "nicht aufgerufen — nur Claude beantwortet Fragen. ",
                    _open("ob/wann sich das ändert, legt der Betreiber fest."),
                ),
                _point(
                    "Sprachausgabe und Spracherkennung:",
                    "laufen lokal auf dem Server des Betreibers (Piper und "
                    "Chatterbox für die Sprachausgabe; lokale "
                    "Spracherkennung). Keine Übertragung an einen externen "
                    "Sprachanbieter.",
                ),
                _said(
                    "Keine Weitergabe an weitere Dritte, kein Verkauf von "
                    "Daten, keine Werbung, kein Tracking, keine Analyse-Tools.",
                ),
            ),
        ),
        LegalSection(
            heading="4. Speicherort",
            paragraphs=(
                _said(
                    "Benutzerkonten, Sitzungen, gespiegelte Decks und gebaute "
                    "Vorträge liegen lokal auf dem vom Betreiber betriebenen "
                    "Server — nicht in der Cloud eines Drittanbieters. Die "
                    "einzige Ausnahme ist die in Abschnitt 3 genannte "
                    "Übertragung an Anthropic für die CoPresenter-Antwort.",
                ),
            ),
        ),
        LegalSection(
            heading="5. Speicherdauer",
            listed=True,
            paragraphs=(
                _point(
                    "Benutzerkonten:",
                    "bis zur Löschung durch einen Administrator.",
                ),
                _point(
                    "Sitzungen:",
                    "Zeile gelöscht beim Abmelden. ",
                    _open(
                        "eine nie abgemeldete Sitzung wird im Code nicht "
                        "automatisch gelöscht.",
                    ),
                ),
                _point(
                    "Fehlgeschlagene Anmeldeversuche:",
                    _open("keine automatische Löschung im Code hinterlegt."),
                ),
                _point(
                    "Einmalig angezeigte Quellen-Zugriffsschlüssel:",
                    "spätestens nach 24 Stunden automatisch entfernt.",
                ),
                _point(
                    "Decks und Builds:",
                    "bis die Quelle entfernt wird oder der Ordner aus Git "
                    "verschwindet.",
                ),
                _point(
                    "CoPresenter-Audio:",
                    "nicht gespeichert, nur für die Antwortdauer gestreamt.",
                ),
                _point("Server-Protokolle:", _open("siehe Abschnitt 2.")),
            ),
        ),
        LegalSection(
            heading="6. Ihre Rechte",
            paragraphs=(
                _said(
                    "Auskunft, Berichtigung, Löschung, Einschränkung der "
                    "Verarbeitung, Datenübertragbarkeit, Widerspruch und "
                    "Beschwerde bei einer Aufsichtsbehörde. Zur Ausübung wenden "
                    "Sie sich an die im ",
                    _IMPRESSUM,
                    " genannte Kontaktadresse.",
                ),
            ),
        ),
        LegalSection(
            heading="7. Stand",
            paragraphs=(
                _said(
                    _open("Datum wird bei der Freigabe durch den Betreiber gesetzt."),
                ),
            ),
        ),
    ),
)
