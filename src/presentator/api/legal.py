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
                    "Beim Login setzen wir ein Sitzungs-Cookie (serverseitig "
                    "signiert, HttpOnly, SameSite=Lax, nur über HTTPS) und ein "
                    "zweites, von JavaScript lesbares Cookie ausschließlich zum "
                    "Fälschungsschutz (CSRF, kein Tracking). Gespeichert werden "
                    "Sitzungs-ID, Benutzer-ID, IP-Adresse, User-Agent und der "
                    "Zeitpunkt der letzten Aktivität. Eine Sitzung gilt nach "
                    f"{_IDLE_HOURS} Stunden Inaktivität als beendet; die Zeile "
                    "selbst wird beim Abmelden gelöscht.",
                ),
                _point(
                    "Fehlgeschlagene Anmeldeversuche:",
                    "Benutzername, IP-Adresse und Zeitpunkt, zum Schutz vor "
                    "automatisiertem Ausprobieren (nach "
                    f"{FAILURES_BEFORE_THROTTLE} Fehlversuchen wird gedrosselt).",
                ),
                _point(
                    "Decks und Quellen:",
                    "Der Inhalt der von Ihnen verbundenen Git-Quellen "
                    "(Folientitel, Folieninhalt, Sprechernotizen) wird auf den "
                    "Server gespiegelt und zu Vorträgen gebaut. Ein "
                    "Lesezugriffsschlüssel pro Quelle (z. B. ein SSH-Deploy-Key) "
                    "wird verschlüsselt gespeichert und kann nach der Eingabe "
                    "nur erneuert, nie wieder angezeigt werden.",
                ),
                _point(
                    "CoPresenter (Sprachfunktion):",
                    "Ihre gesprochene Frage wird lokal auf dem Server in Text "
                    "umgewandelt; die Audio-Rohdaten selbst werden dabei nicht "
                    "gespeichert. Der erkannte Text, die aktuell gezeigte Folie "
                    "und der vollständige Inhalt des Decks (inklusive "
                    "Sprechernotizen) werden an den unten genannten KI-Anbieter "
                    "gesendet, um eine Antwort zu erzeugen. Die gesprochene "
                    "Antwort wird lokal erzeugt und nur für die Wiedergabe "
                    "gestreamt, nicht dauerhaft gespeichert.",
                ),
                _point(
                    "Server-Protokolle:",
                    "Die Anwendung schreibt Betriebsprotokolle. ",
                    _open(
                        "Aufbewahrungsdauer und Speicherort der "
                        "Server-Protokolle sind nicht im Anwendungscode "
                        "festgelegt; das liegt an der Server-Konfiguration des "
                        "Betreibers.",
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
                    "Für die CoPresenter-Antworten wird aktuell ausschließlich "
                    "Claude verwendet — über den eigenen Claude-Zugang des "
                    "Betreibers (die installierte claude-Kommandozeile), nicht "
                    "über einen pro Anfrage übertragenen API-Schlüssel. "
                    "Übertragen werden dabei die gesprochene Frage (als Text), "
                    "die aktuelle Folie und der gesamte Deckinhalt. ",
                    _open(
                        "Anthropics eigene Aufbewahrungs- und "
                        "Verarbeitungspraxis für diese Anfragen liegt außerhalb "
                        "dieses Codes und richtet sich nach Anthropics eigenen "
                        "Bedingungen und den Kontoeinstellungen des Betreibers.",
                    ),
                ),
                _point(
                    "Grok, Codex:",
                    "Die Technik ist für weitere KI-Anbieter vorbereitet, wird "
                    "aber vom CoPresenter-Code aktuell nicht aufgerufen — nur "
                    "Claude beantwortet Fragen. ",
                    _open(
                        "ob und wann das künftig geändert wird, legt der "
                        "Betreiber fest; diese Erklärung wird dann entsprechend "
                        "ergänzt.",
                    ),
                ),
                _point(
                    "Sprachausgabe und Spracherkennung:",
                    "laufen auf dem Server des Betreibers selbst (laut "
                    "Mockup-Dokumentation Piper und Chatterbox für die "
                    "Sprachausgabe; die Spracherkennung läuft ebenfalls lokal). "
                    "Es findet keine Übertragung an einen externen "
                    "Sprachanbieter statt.",
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
                    "Benutzerkonten, Sitzungen, gespiegelte Decks und die "
                    "gebauten Vorträge liegen lokal auf dem vom Betreiber "
                    "betriebenen Server — nicht in der Cloud eines "
                    "Drittanbieters. Die einzige Ausnahme ist die in Abschnitt "
                    "3 genannte Übertragung an Anthropic für die "
                    "CoPresenter-Antwort.",
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
                    "Die Zeile wird beim Abmelden gelöscht. ",
                    _open(
                        "Für eine Sitzung, die nie aktiv abgemeldet wird, "
                        "löscht der Code die Zeile nicht automatisch — sie "
                        "bleibt bestehen, auch wenn sie nach "
                        f"{_IDLE_HOURS} Stunden Inaktivität nicht mehr als "
                        "gültig gilt.",
                    ),
                ),
                _point(
                    "Fehlgeschlagene Anmeldeversuche:",
                    _open(
                        "Der Code löscht diese Zeilen nicht automatisch; es "
                        "gibt keine im Code hinterlegte Frist.",
                    ),
                ),
                _point(
                    "Einmalig angezeigte Quellen-Zugriffsschlüssel "
                    "(SSH-Deploy-Key-Entwürfe):",
                    "werden spätestens nach 24 Stunden automatisch entfernt.",
                ),
                _point(
                    "Decks und Builds:",
                    "bleiben erhalten, bis die zugehörige Quelle entfernt wird "
                    "oder der Ordner aus dem Git-Repository verschwindet.",
                ),
                _point(
                    "CoPresenter-Audio:",
                    "wird nicht gespeichert, nur für die Dauer der Antwort gestreamt.",
                ),
                _point("Server-Protokolle:", _open("siehe Abschnitt 2.")),
            ),
        ),
        LegalSection(
            heading="6. Ihre Rechte",
            paragraphs=(
                _said(
                    "Sie haben das Recht auf Auskunft über Ihre gespeicherten "
                    "Daten, Berichtigung unrichtiger Daten, Löschung, "
                    "Einschränkung der Verarbeitung, Datenübertragbarkeit, "
                    "Widerspruch gegen die Verarbeitung und Beschwerde bei "
                    "einer Aufsichtsbehörde. Zur Ausübung wenden Sie sich an "
                    "die im ",
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
