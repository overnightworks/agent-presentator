"""The Datenschutzerklärung: the one owner of its words, rendered by its own page.

It is German whatever language a reader chose, because it is the operator's
legal declaration, not interface wording a catalog translates (ADR 0012). The
Impressum is not here: every page links the operator's one Impressum instead of
carrying a copy (ADR 0015).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from webauth.oidc import PENDING_LOGIN_MAX_AGE_SECONDS

from presentator.application.decks import DRAFT_MAX_AGE
from presentator.application.identity import SESSION_LIFETIME, SESSION_SWEEP_INTERVAL

_SECONDS_PER_HOUR: Final = 3600
_SECONDS_PER_MINUTE: Final = 60


class Emphasis(StrEnum):
    """How a phrase stands out from the sentence around it."""

    PLAIN = "plain"
    LABEL = "label"
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


_IMPRESSUM: Final = Phrase("Impressum", Emphasis.IMPRESSUM)
_SESSION_HOURS: Final = int(SESSION_LIFETIME.total_seconds()) // _SECONDS_PER_HOUR
_PENDING_MINUTES: Final = PENDING_LOGIN_MAX_AGE_SECONDS // _SECONDS_PER_MINUTE
_SWEEP_MINUTES: Final = (
    int(SESSION_SWEEP_INTERVAL.total_seconds()) // _SECONDS_PER_MINUTE
)
_DRAFT_HOURS: Final = int(DRAFT_MAX_AGE.total_seconds()) // _SECONDS_PER_HOUR
_STAND: Final = "23. Oktober 2026"
_BASIS_CONTRACT: Final = "Art. 6 Abs. 1 lit. b DSGVO (hilfsweise lit. f)"
_BASIS_INTEREST: Final = "Art. 6 Abs. 1 lit. f DSGVO"

DATENSCHUTZ: Final = LegalDocument(
    language_tag="de",
    title="Datenschutzerklärung",
    introduction=_said(
        "Diese Erklärung beschreibt, welche Daten Presentator verarbeitet, wohin "
        "sie gelangen und wie lange sie bleiben."
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
                    "Kennung (Subject-ID) des Keycloak-Kontos, Benutzername und "
                    "Rolle (Admin/Nutzer) sowie Ihre Einstellungen für Sprache "
                    "und Darstellung. Presentator speichert kein Passwort. Die "
                    "Anmeldung läuft über Keycloak unter auth.hallucinai.de, das "
                    "der Betreiber betreibt. Zweck · Rechtsgrundlage: Konto und "
                    f"Einstellungen bereitstellen, {_BASIS_CONTRACT}.",
                ),
                _point(
                    "Sitzung und Cookies:",
                    "Wir setzen vier Cookie-Arten. Bei jedem Start der Anmeldung "
                    "setzt Presentator __Host-oidc_pending_… (signiert, "
                    "HttpOnly, nur über HTTPS, "
                    f"{_PENDING_MINUTES} Minuten gültig): Es hält den Zustand "
                    "der Anmeldung für die Rückkehr von Keycloak und wird bei "
                    "der Rückkehr gelöscht; kommt die Rückkehr ohne "
                    "verwertbaren Zustand, bleibt es, bis seine "
                    f"{_PENDING_MINUTES} Minuten ablaufen. Nach der Anmeldung "
                    "folgen "
                    "__Host-session_id als Sitzungs-Cookie (HttpOnly, "
                    "SameSite=Lax, nur über HTTPS) und __Host-csrf_token, das "
                    "von JavaScript lesbar ist und nur dem Fälschungsschutz "
                    "(CSRF) dient, nicht dem Tracking. "
                    "__Secure-oidc_id_token_hint hält das ID-Token von "
                    "Keycloak für das Abmelden; der Browser sendet es nur an "
                    "/logout (Pfad /logout, HttpOnly, nur über HTTPS). Das "
                    "Token enthält Ihre Subject-ID, Ihre Gruppen und, soweit "
                    "in Keycloak hinterlegt, Ihren Benutzernamen, Ihren "
                    "Namen und Ihre E-Mail-Adresse. Beim Abmelden wird "
                    "dieses Cookie gelöscht. Ist es dabei noch vorhanden und "
                    "verwendbar, gibt Presentator sein ID-Token als Hinweis an "
                    "Keycloak weiter, damit Keycloak ohne Rückfrage abmelden "
                    "kann. Der Hinweis steht in der Adresse, die der Browser "
                    "aufruft, und kann so im Browserverlauf landen. "
                    "Presentator beendet beim Abmelden sofort die Sitzung in "
                    "Presentator und leitet den Browser zu Keycloak weiter, um "
                    "dort auch die Anmeldung zu beenden; Keycloak fragt dabei "
                    "gegebenenfalls nach. Die drei "
                    "Cookies nach der Anmeldung "
                    f"sind {_SESSION_HOURS} Stunden gültig und "
                    "werden danach nicht erneuert. Die Vorträge selbst "
                    "liefert ein eigener Host aus (der Talk-Host): Dort "
                    "meldet Presentator Sie selbst an und setzt dieselben "
                    "Cookies noch einmal, nur für diesen Host; sein "
                    "__Host-session_id ist ein eigenes Sitzungs-Cookie, das "
                    "bei der Anmeldung dort entsteht und technisch "
                    "erforderlich ist. Gespeichert werden "
                    "Sitzungs-ID, Benutzer-ID, der Zeitpunkt der Anmeldung "
                    "und des Ablaufs sowie das Erneuerungs-Token "
                    "(Refresh-Token), das Keycloak bei der Anmeldung und bei "
                    "jeder Erneuerung ausstellt. Es liegt unverschlüsselt in der "
                    "Sitzungszeile, ist ohne das Client-Geheimnis von "
                    "Presentator bei Keycloak nicht verwendbar und wird mit "
                    "der Sitzung gelöscht. Für jede CoPresenter-Frage holt "
                    "Presentator ein frisches Zugriffstoken bei Keycloak und "
                    "gibt es an den Agent-Dienst weiter; gespeichert wird es "
                    "nicht. Eine "
                    f"Sitzung endet {_SESSION_HOURS} Stunden nach der Anmeldung. "
                    "Zweck · Rechtsgrundlage: angemeldet bleiben, "
                    f"{_BASIS_CONTRACT}; die Cookies "
                    "selbst sind nach § 25 Abs. 2 Nr. 2 TDDDG ohne Einwilligung "
                    "zulässig.",
                ),
                _point(
                    "Lokaler Speicher des Browsers:",
                    "Die Vortragsansicht (Slidev) legt Einstellungen wie Kamera, "
                    "Mikrofon und Export-Hinweise unter Schlüsseln mit dem "
                    "Anfang slidev- im lokalen Speicher Ihres Browsers ab. "
                    "Einige davon werden beim Öffnen mit Standardwerten "
                    "angelegt. Sie tragen keine Kennung und werden nie an den "
                    "Server gesendet. Zweck · Rechtsgrundlage: Einstellungen, "
                    "die Sie selbst wählen, so darstellen, wie Sie sie "
                    "eingestellt haben, § 25 Abs. 2 Nr. 2 TDDDG.",
                ),
                _point(
                    "Decks und Quellen:",
                    "Ein Administrator verbindet Git-Quellen. Der Inhalt "
                    "(Folientitel, Folieninhalt, Sprechernotizen) wird auf den "
                    "Server gespiegelt und zu Vorträgen gebaut; der Spiegel "
                    "behält auch die Namen und E-Mail-Adressen der "
                    "Mitwirkenden aus dem Git-Verlauf. Zugangsdaten einer "
                    "Quelle (z. B. ein SSH-Deploy-Key) werden verschlüsselt "
                    "gespeichert und nach der Eingabe nur erneuert, nie wieder "
                    "angezeigt; Webhook-Geheimnisse werden nur als Hash "
                    "gespeichert. Zweck · Rechtsgrundlage: Vorträge "
                    f"bereitstellen, {_BASIS_CONTRACT}.",
                ),
                _point(
                    "CoPresenter (Sprachfunktion):",
                    "Ihre gesprochene Frage wird auf dem Server in Text "
                    "umgewandelt; die Audio-Rohdaten werden nicht gespeichert. "
                    "Stellen Sie im Vortrag eine Frage, sendet Presentator sie "
                    "mit dem Titel und dem Text des gefragten Decks an den "
                    "Agent-Dienst von hallucinai. Was der Dienst damit tut, "
                    "beschreibt sein Abschnitt in der Datenschutzerklärung von "
                    "hallucinai. "
                    "Solange der CoPresenter zuhört, wird Sprache im "
                    "Raum fortlaufend erkannt, und jede erkannte Äußerung wird "
                    "als Frage gesendet, außer während der "
                    "CoPresenter spricht oder wenn ein Echo erkannt wird; "
                    "niemand wählt die Fragen aus. Die "
                    "gesprochene Antwort wird "
                    "auf dem Server erzeugt und nur für die Wiedergabe "
                    "gestreamt. Zweck · Rechtsgrundlage: der Vortrag "
                    "des Betreibers mit gesprochenen Fragen, "
                    f"{_BASIS_INTEREST}; eine Einwilligung wird dafür nicht "
                    "in Anspruch genommen.",
                ),
                _point(
                    "Server-Protokolle:",
                    "Der Server schreibt kein Zugriffsprotokoll. Wenn ein "
                    "Konto angelegt wird oder sich seine Rolle ändert, "
                    "schreibt er eine Prüfzeile mit Ereignisart, "
                    "Keycloak-Subject-ID und Rolle, nie mit Benutzername oder "
                    "Token. Eine "
                    "Aufbewahrungsdauer ist nicht festgelegt. Zweck · "
                    "Rechtsgrundlage: Betrieb und Fehlersuche, "
                    f"{_BASIS_INTEREST}.",
                ),
            ),
        ),
        LegalSection(
            heading="3. Empfänger und externe Anbieter",
            listed=True,
            paragraphs=(
                _point(
                    "Keycloak:",
                    "Die Anmeldung läuft über Keycloak unter "
                    "auth.hallucinai.de, das der Betreiber betreibt.",
                ),
                _point(
                    "Cloudflare:",
                    "Der Zugang zur Anwendung läuft über Cloudflare. Die "
                    "verschlüsselte Verbindung endet dort, daher läuft auch die "
                    "Mikrofon-Verbindung des CoPresenters durch Cloudflare.",
                ),
                _point(
                    "Git-Hosts der verbundenen Quellen:",
                    "erhalten die Abrufe, mit denen der Server die Quellen spiegelt.",
                ),
                _point(
                    "Sprachausgabe und Spracherkennung:",
                    "Die Sprachausgabe (Piper, Chatterbox, Qwen3-TTS, VoxCPM 2, "
                    "NVIDIA Magpie) und die Spracherkennung (faster-whisper) "
                    "laufen auf dem Server des Betreibers.",
                ),
            ),
        ),
        LegalSection(
            heading="4. Speicherort",
            paragraphs=(
                _said(
                    "Benutzerkonten, Sitzungen, gespiegelte Decks und die "
                    "gebauten Vorträge liegen auf dem vom Betreiber "
                    "betriebenen Server. Die in Abschnitt 3 genannten "
                    "Empfänger erhalten Daten aus den dort genannten Gründen.",
                ),
            ),
        ),
        LegalSection(
            heading="5. Speicherdauer",
            listed=True,
            paragraphs=(
                _point(
                    "Benutzerkonten:",
                    "Der Eintrag bleibt bestehen; keine Funktion löscht oder "
                    "exportiert eine Person.",
                ),
                _point(
                    "Sitzungen:",
                    "Die Zeile endet beim Abmelden; abgelaufene Sitzungen "
                    f"löscht der Dienst alle {_SWEEP_MINUTES} Minuten.",
                ),
                _point(
                    "Einmalig angezeigte Quellen-Zugriffsschlüssel "
                    "(SSH-Deploy-Key-Entwürfe):",
                    f"werden rund {_DRAFT_HOURS} Stunden nach Erzeugung beim "
                    "nächsten Abgleich gelöscht.",
                ),
                _point(
                    "Decks und Builds:",
                    "Die Decks eines entfernten Ordners werden nur markiert; "
                    "ein neuer Build lässt den alten bestehen. Das Entfernen "
                    "einer Quelle löscht ihren Spiegel, ihre Decks und deren "
                    "aktive Builds.",
                ),
                _point(
                    "CoPresenter-Audio:",
                    "wird nicht gespeichert, nur für die Dauer der Antwort gestreamt.",
                ),
                _point(
                    "Server-Protokolle:",
                    "Eine Aufbewahrungsdauer ist nicht festgelegt.",
                ),
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
                    "einer Aufsichtsbehörde. Keine Funktion in Presentator "
                    "löscht oder exportiert eine Person; wenden Sie sich zur "
                    "Ausübung an die im ",
                    _IMPRESSUM,
                    " genannte Kontaktadresse.",
                ),
            ),
        ),
        LegalSection(
            heading="7. Stand",
            paragraphs=(_said(f"Stand: {_STAND}"),),
        ),
    ),
)
