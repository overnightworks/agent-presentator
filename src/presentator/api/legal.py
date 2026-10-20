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
_STAND: Final = "20. Oktober 2026"
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
                    "Wir setzen vier Cookies. Beim Start der Anmeldung setzt "
                    "Presentator __Host-oidc_pending_… (signiert, HttpOnly, "
                    "nur über HTTPS, "
                    f"{_PENDING_MINUTES} Minuten gültig): Es hält den Zustand "
                    "der Anmeldung für die Rückkehr von Keycloak und wird bei "
                    "der Rückkehr gelöscht. Nach der Anmeldung folgen "
                    "__Host-session_id als Sitzungs-Cookie (HttpOnly, "
                    "SameSite=Lax, nur über HTTPS) und __Host-csrf_token, das "
                    "von JavaScript lesbar ist und nur dem Fälschungsschutz "
                    "(CSRF) dient, nicht dem Tracking. "
                    "__Secure-oidc_id_token_hint hält das ID-Token von "
                    "Keycloak für das Abmelden; der Browser sendet es nur an "
                    "/logout (Pfad /logout, HttpOnly, nur über HTTPS). Das "
                    "Token enthält Ihre Subject-ID, Ihren Benutzernamen und "
                    "Ihre Gruppen, mit dem Profil-Scope möglicherweise auch "
                    "Ihren Namen. Presentator beendet beim Abmelden nur die "
                    "Sitzung in Presentator, nicht die bei Keycloak. Die drei "
                    "Cookies nach der Anmeldung "
                    f"sind {_SESSION_HOURS} Stunden gültig und "
                    "werden danach nicht erneuert. Gespeichert werden "
                    "Sitzungs-ID, Benutzer-ID und der Zeitpunkt der Anmeldung "
                    "und des Ablaufs. Eine "
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
                    "Von der Folie, die er zeigt, sendet der Browser nur die "
                    "Nummer, daneben das Mikrofon-Audio und den erkannten Text "
                    "der Frage. Der CoPresenter nimmt die Folie mit dieser "
                    "Nummer aus dem Deck, mit dem er gestartet wurde, falls "
                    "dieses Deck sie hat. Der erkannte Text, der Inhalt dieser "
                    "Folie und jede Folie dieses Decks mit ihren Notizen "
                    "werden an Anthropic gesendet, um eine Antwort zu "
                    "erzeugen; dieses Deck kann vom Deck im Browser "
                    "abweichen. Solange der CoPresenter zuhört, wird Sprache im "
                    "Raum fortlaufend erkannt, und jede erkannte Äußerung wird "
                    "als Frage an Anthropic gesendet, außer während der "
                    "CoPresenter spricht oder wenn ein Echo erkannt wird; "
                    "niemand wählt die Fragen aus. Der CoPresenter "
                    "steht derzeit nur dem Betreiber zur Verfügung. Die "
                    "gesprochene Antwort wird "
                    "auf dem Server erzeugt und nur für die Wiedergabe "
                    "gestreamt. Zweck · Rechtsgrundlage: der Vortrag "
                    "des Betreibers mit gesprochenen Fragen, "
                    f"{_BASIS_INTEREST}; eine Einwilligung wird dafür nicht "
                    "in Anspruch genommen.",
                ),
                _point(
                    "Server-Protokolle:",
                    "Der Server schreibt Zugriffsprotokolle. Darin steht die "
                    "Adresse des Docker-Gateways, nicht die Ihres Geräts. Wenn "
                    "ein Konto angelegt wird oder sich seine Rolle ändert, "
                    "schreibt der Server außerdem eine Prüfzeile mit "
                    "Ereignisart, Keycloak-Subject-ID und Rolle, nie mit "
                    "Benutzername oder Token. Eine "
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
                    "Anthropic Ireland, Limited:",
                    "Für die CoPresenter-Antworten wird Claude verwendet, über "
                    "den eigenen Claude-Zugang des Betreibers (die installierte "
                    "claude-Kommandozeile). Anthropic ist dafür ein eigener "
                    "Verantwortlicher. Übertragen werden die gesprochene Frage "
                    "als Text, der Inhalt der Folie mit der Nummer, die der Browser "
                    "meldet, aus dem Deck, mit dem der CoPresenter gestartet wurde "
                    "(falls dieses Deck sie hat), und jede Folie dieses Decks mit "
                    "ihren Notizen.",
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
                    "Kopien der Claude-Software:",
                    "Frage, Deck und Antwort bleiben im Klartext bei der "
                    "Claude-Software auf dem Server des Betreibers; deren "
                    "Standardeinstellung sind 30 Tage, eine Obergrenze ist "
                    "nicht belegt.",
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
