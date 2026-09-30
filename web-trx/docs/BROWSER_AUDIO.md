# Browser-Audio für TX und Server-Watchdog

Web-TRX nimmt das Audio für die Sprachmodi (FM, SSB, LSB, M17, RADE) aus dem
**Browser**: Ein Eingang des Rechners, auf dem der Browser läuft, oder eine
Audiodatei geht als 20-ms-PCM-Blöcke über den WebSocket zum Server. Der
Server moduliert und sendet damit.

Ein **Watchdog auf dem Server** beendet jede Aussendung sofort, wenn der
Browser nicht mehr reagiert.

## Audioquellen (TX-Panel → „Audioquelle“)

- **Eingang:** jedes Eingabegerät des Browser-Rechners, etwa ein Mikrofon,
  Line-in, eine USB-Soundkarte oder ein virtuelles Kabel.
  - Die Gerätenamen zeigt der Browser erst, nachdem die Seite einmal
    aufnehmen durfte (Knopf „Geräte freigeben“).
  - **Sprachverarbeitung des Browsers** umfasst Echo- und
    Rauschunterdrückung sowie die automatische Pegelregelung. Für ein
    Mikrofon sinnvoll, bei Line-Signalen oder einer Soundkarte **aus**,
    sonst verbiegt der Browser das Signal. Die Einstellung wird pro Gerät
    gespeichert.
  - Verschwindet das Gerät während einer Aussendung (Stecker gezogen), wird
    PTT sofort freigegeben.
- **Datei:** WAV, MP3, OGG oder was der Browser sonst dekodiert, höchstens
  50 MB und 10 min.
  - Die Datei spielt ab ihrem Anfang, solange PTT gedrückt ist.
  - Ohne „Schleife“ endet die Aussendung am Dateiende von selbst.
  - Es gilt weiter der 180-s-Sendezeitbegrenzer des Servers.
  - Stereo wird zu Mono gemischt.
- **Eingangspegel** −30 … +20 dB, dazu die Spitzenpegelanzeige. Über 0 dBFS
  wird geclippt; der Server hat danach noch Kompressor und Limiter.
- Die Auswahl merkt sich der Browser (localStorage), eine Datei aber nicht.
  Solange PTT gedrückt ist, lässt sich die Quelle nicht ändern.
- **Voraussetzungen:**
  - HTTPS oder `localhost`, sonst gibt der Browser kein Audio frei.
  - Die Geräteauswahl funktioniert in Chromium, Chrome, Edge und Firefox.

## Watchdogs

| Was | Wann | Folge |
|---|---|---|
| **Herzschlag** (neu, `session.py`) | Der Server schickt jede Sekunde `hb`, die Seite antwortet `hb_ack` direkt aus `ws.onmessage` (JavaScript-Ereignisschleife). Antwortet **5 s** lang kein verbundener Browser, während gesendet wird oder etwas scharfgeschaltet ist … | JS8-Automatik aus, Aussendung beendet (`ptt off`, Grund `client_unresponsive`; klappt das nicht: NOTAUS), stumme Verbindungen werden mit Code 4000 geschlossen. |
| Audio-Watchdog (`radio_backend.py`) | Sprachmodus getastet, 1,5 s kein Audio vom Browser | Enttasten |
| Sendezeitbegrenzer | 180 s (`WEB_TRX_TX_TIMEOUT_S`) | Enttasten |
| Browser geschlossen | WebSocket zu | sofort beendet, nach 10 s Geräte getrennt |
| Fokusverlust | Fenster verliert den Fokus, Tab verborgen oder verlassen | PTT frei (eine gehaltene Leertaste bekommt sonst nie ein `keyup`) |

**Einstellungen:**
- `WEB_TRX_CLIENT_TIMEOUT_S` (Standard 5 s): ab wann ein Browser als
  unreaktiv gilt.
- `WEB_TRX_HB_INTERVAL_S` (Standard 1 s): Abstand der Herzschläge.

**Hinweise:**
- `hb_ack` zählt nicht als Bedienung; der Idle-Watchdog der JS8-Automatik
  läuft weiter.
- Browser drosseln Timer in Hintergrund-Tabs, stellen WebSocket-Nachrichten
  aber weiter zu. Die Antwort kommt deshalb auch aus einem Hintergrund-Tab.
- **Friert** der Browser einen Tab ein (Energiespar- oder Speichersparmodus),
  bleibt sie aus. Dann endet eine laufende Aussendung, das ist gewollt: Es
  ist niemand mehr da.
- Bei eingefrorener Seite greift in Sprachmodi schon der Audio-Watchdog
  nach 1,5 s. Der Herzschlag schützt vor allem die servergesteuerten
  Aussendungen: FT8-/JS8-Serien, POCSAG, RTTY, Digitext und die
  JS8-Automatik.

## Funktest (30.09.2026, Freigabe des Betreibers)

**Aufbau:**
- Separater Web-TRX-Testserver (GnuRadioBackend, Port 8398, nicht die
  Produktivinstanz).
- Headless-Chromium steuert die Seite (Playwright), die Seite wird mit
  einer Endlosschleife in JavaScript eingefroren.
- **Sender:** Pluto+, TX-Dämpfung **40 dB** (`hardwaregain` −40,000 dB
  während der Aussendung).
- **Empfang:** RTL-SDR daneben, `rtl_fm`.
- Der Pluto-Zustand wurde alle 0,2 s mit `iio_attr` abgefragt.

| UTC | Frequenz | Inhalt / Quelle | Ergebnis |
|---|---|---|---|
| 12:51:54,6–12:52:00,0 | 145,3375 MHz FM | `da2jh-test.wav` (Testansage DA2JH), Quelle „Datei“ ohne Schleife | Am Dateiende nach 5,3 s von selbst enttastet. Der RTL-SDR empfängt die Ansage: 94 % der Energie im Sprachband, Korrelation mit dem Original 0,53 (nach De-Emphasis). |
| 12:53:01,6–12:53:05,5 | 145,3375 MHz FM | dieselbe Datei in Schleife; Seite ab 12:53:04,0 eingefroren | **1,5 s** nach dem Einfrieren enttastet (Audio-Watchdog). |
| 12:53:14,5 (scharf) | 144,178 MHz USB, JS8 | `DA2JH: @ALLCALL WD TEST`; Seite sofort 9 s eingefroren | Nach 5 s ohne Antwort abgebrochen, **noch vor dem Tasten**; keine Aussendung (TX-Log leer). |
| 12:54:27,7–12:54:32,2 | 144,178 MHz + 1500 Hz, JS8 NORMAL | `DA2JH: @ALLCALL WD TEST`; Seite ab 12:54:27,9 eingefroren, Rahmenbeginn 12:54:30,5 | **Mitten im Rahmen** abgebrochen, ≈5 s nach der letzten Antwort (Herzschlag-Watchdog). |

- Nach jeder Aussendung: −89,75 dB, LO aus.
- **Beobachtung:** Beim Verbinden bzw. Moduswechsel des TX lief der Pluto-LO
  jeweils etwa 0,4 s bei −89,75 dB (maximale Dämpfung, kein Nutzsignal),
  z. B. 12:51:53,2–53,8. Das ist das bisherige Verhalten beim
  Flowgraph-Start und hat mit dieser Änderung nichts zu tun.
