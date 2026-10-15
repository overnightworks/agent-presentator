# Datenschutzerklärung — Presentator (Entwurf für #196)

Entwurf, aus dem Code abgeleitet (Stand dieser Branch, Commit 5dafa4b). Der
Betreiber muss diesen Text prüfen und freigeben, bevor er online geht (Linie
3). Jede Stelle, die sich nicht aus dem Code ergibt, ist mit
`[offen: …]` markiert statt geraten.

Dieser Text geht unverändert auch auf das Datenschutz-Artboard in
`m0-lobby.html`.

---

## 1. Verantwortlicher

Verantwortlich für diese Anwendung ist der Betreiber. Kontaktdaten: siehe
[Impressum](https://hallucinai.de/impressum).

## 2. Welche Daten wir verarbeiten

- **Benutzerkonto:** Benutzername, Rolle (Admin/Nutzer) und ein mit
  Argon2id gehashtes Passwort. Konten legt ausschließlich ein Administrator
  an; es gibt keine Selbstregistrierung.
- **Sitzungen und Cookies:** Beim Login setzen wir ein Sitzungs-Cookie
  (serverseitig signiert, `HttpOnly`, `SameSite=Lax`, nur über HTTPS) und
  ein zweites, von JavaScript lesbares Cookie ausschließlich zum
  Fälschungsschutz (CSRF, kein Tracking). Gespeichert werden Sitzungs-ID,
  Benutzer-ID, IP-Adresse, User-Agent und der Zeitpunkt der letzten Aktivität.
  Eine Sitzung gilt nach 12 Stunden Inaktivität als beendet; die Zeile selbst
  wird beim Abmelden gelöscht.
- **Fehlgeschlagene Anmeldeversuche:** Benutzername, IP-Adresse und
  Zeitpunkt, zum Schutz vor automatisiertem Ausprobieren (nach 5 Fehlversuchen
  wird gedrosselt).
- **Decks und Quellen:** Der Inhalt der von Ihnen verbundenen Git-Quellen
  (Foliertitel, Folieninhalt, Sprechernotizen) wird auf den Server gespiegelt
  und zu Vorträgen gebaut. Ein Lesezugriffsschlüssel pro Quelle (z. B. ein
  SSH-Deploy-Key) wird verschlüsselt gespeichert und kann nach der Eingabe nur
  erneuert, nie wieder angezeigt werden.
- **CoPresenter (Sprachfunktion):** Ihre gesprochene Frage wird lokal auf
  dem Server in Text umgewandelt; die Audio-Rohdaten selbst werden dabei nicht
  gespeichert. Der erkannte Text, die aktuell gezeigte Folie und der
  vollständige Inhalt des Decks (inklusive Sprechernotizen) werden an den
  unten genannten KI-Anbieter gesendet, um eine Antwort zu erzeugen. Die
  gesprochene Antwort wird lokal erzeugt und nur für die Wiedergabe
  gestreamt, nicht dauerhaft gespeichert.
- **Server-Protokolle:** Die Anwendung schreibt Betriebsprotokolle.
  [offen: Aufbewahrungsdauer und Speicherort der Server-Protokolle sind nicht
  im Anwendungscode festgelegt; das liegt an der Server-Konfiguration des
  Betreibers.]

## 3. Empfänger und externe Anbieter

- **Anthropic (Claude):** Für die CoPresenter-Antworten wird aktuell
  ausschließlich Claude verwendet — über den eigenen Claude-Zugang des
  Betreibers (die installierte `claude`-Kommandozeile), nicht über einen pro
  Anfrage übertragenen API-Schlüssel. Übertragen werden dabei die gesprochene
  Frage (als Text), die aktuelle Folie und der gesamte Deckinhalt. [offen:
  Anthropics eigene Aufbewahrungs- und Verarbeitungspraxis für diese Anfragen
  liegt außerhalb dieses Codes und richtet sich nach Anthropics eigenen
  Bedingungen und den Kontoeinstellungen des Betreibers.]
- **Grok, Codex:** Die Technik ist für weitere KI-Anbieter vorbereitet, wird
  aber vom CoPresenter-Code aktuell nicht aufgerufen — nur Claude beantwortet
  Fragen. [offen: ob und wann das künftig geändert wird, legt der Betreiber
  fest; diese Erklärung wird dann entsprechend ergänzt.]
- **Sprachausgabe und Spracherkennung:** laufen auf dem Server des
  Betreibers selbst (laut Mockup-Dokumentation Piper und Chatterbox für die
  Sprachausgabe; die Spracherkennung läuft ebenfalls lokal). Es findet keine
  Übertragung an einen externen Sprachanbieter statt.
- Keine Weitergabe an weitere Dritte, kein Verkauf von Daten, keine
  Werbung, kein Tracking, keine Analyse-Tools.

## 4. Speicherort

Benutzerkonten, Sitzungen, gespiegelte Decks und die gebauten Vorträge
liegen lokal auf dem vom Betreiber betriebenen Server — nicht in der Cloud
eines Drittanbieters. Die einzige Ausnahme ist die in Abschnitt 3 genannte
Übertragung an Anthropic für die CoPresenter-Antwort.

## 5. Speicherdauer

- **Benutzerkonten:** bis zur Löschung durch einen Administrator.
- **Sitzungen:** Die Zeile wird beim Abmelden gelöscht. [offen: Für eine
  Sitzung, die nie aktiv abgemeldet wird, löscht der Code die Zeile nicht
  automatisch — sie bleibt bestehen, auch wenn sie nach 12 Stunden Inaktivität
  nicht mehr als gültig gilt.]
- **Fehlgeschlagene Anmeldeversuche:** [offen: Der Code löscht diese Zeilen
  nicht automatisch; es gibt keine im Code hinterlegte Frist.]
- **Einmalig angezeigte Quellen-Zugriffsschlüssel (SSH-Deploy-Key-Entwürfe):**
  werden spätestens nach 24 Stunden automatisch entfernt.
- **Decks und Builds:** bleiben erhalten, bis die zugehörige Quelle entfernt
  wird oder der Ordner aus dem Git-Repository verschwindet.
- **CoPresenter-Audio:** wird nicht gespeichert, nur für die Dauer der
  Antwort gestreamt.
- **Server-Protokolle:** [offen: siehe Abschnitt 2.]

## 6. Ihre Rechte

Sie haben das Recht auf Auskunft über Ihre gespeicherten Daten,
Berichtigung unrichtiger Daten, Löschung, Einschränkung der Verarbeitung,
Datenübertragbarkeit, Widerspruch gegen die Verarbeitung und Beschwerde bei
einer Aufsichtsbehörde. Zur Ausübung wenden Sie sich an die im
[Impressum](https://hallucinai.de/impressum) genannte Kontaktadresse.

## 7. Stand

[offen: Datum wird bei der Freigabe durch den Betreiber gesetzt.]
