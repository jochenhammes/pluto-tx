# Web-TRX

Einheitliche Weboberfläche für `pluto-tx`/`pluto-advanced-rx`
(Sende-/Empfangs-Software für ADALM-PLUTO, HackRF One und RTL-SDR). Ein
lizenzierter Funkamateur bedient die am Server angeschlossenen SDRs
vollständig über den Browser — Senden und Empfangen, inklusive Wasserfall/
Spektrum, Audio und Digimodes.

Web-TRX ist Teil des pluto-tx-Repositorys (dieser Ordner `web-trx/`) und
steht gleichwertig neben `pluto-tx`, `pluto-advanced-rx` und `pluto-cli`.
Das Backend importiert den Python-Code direkt aus diesem Checkout. Starten
und Stoppen geht per `web-trx start|stop`, mit den Skripten unter
`scripts/` oder mit der Zeile „Web-TRX“ in der TX- und der RX-App.

**Stand:**
- **Empfang:** FM, SSB (USB/LSB) und M17 mit Audio im Browser. POCSAG wird dekodiert.
- **APRS:** Stationen auf der Karte und Nachrichten, nur über Funk.
- **Wasserfall:** echtes Zoom-FFT, Mittelung, automatischer Pegel.
- **Senden:** FM, SSB (USB/LSB) und M17 mit Audio aus dem Browser, mit Audio-Aufbereitung und Leistungsregler.
  - **Quelle wählbar:** jeder Eingang des Browser-Rechners (Mikrofon, Line-in, USB-Soundkarte, virtuelles Kabel;
    Sprachverarbeitung des Browsers abschaltbar) oder eine Audiodatei, die am Ende von selbst enttastet.
  - Details: [`docs/BROWSER_AUDIO.md`](docs/BROWSER_AUDIO.md).
- **POCSAG senden:** eine Nachricht an eine RIC.
- **JTTY (experimentell):** Tastaturmodus aus WSJT-X, Nachrichten wachsen beim Empfang mit, Senden von Hand.
- **APRS senden:** Bake oder Nachricht, ein Paket je Klick.
- **MeshCore:** Nodes auf der Karte, Kanäle und Direktnachrichten, Empfang und Senden von Hand.
- **Sicherheit:**
  - NOTAUS;
  - automatisches Enttasten, wenn das Browser-Audio abreißt;
  - **Server-Watchdog:** Antwortet der Browser 5 s lang nicht auf den Herzschlag, endet jede Aussendung, auch
    FT8/JS8/POCSAG und die JS8-Automatik;
  - PTT wird frei, wenn die Seite den Fokus verliert;
  - Sende-Zeitbegrenzung, TX-Log.

Projektplan (Architektur, Betriebsarten, Meilensteine, aktueller Stand):
[`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md).
Debug-/Teststrategie und bekannte Stolpersteine: [`docs/DEBUGGING.md`](docs/DEBUGGING.md).

## Installation

Voraussetzung ist ein eingerichtetes pluto-tx (`./install.sh` im
Repo-Wurzelverzeichnis; optional `install-m17.sh`, `install-rade.sh`,
`install-ft8.sh`). Kurz: `pluto-tx` bzw. `pluto-advanced-rx` starten und
funktionieren. Web-TRX nutzt dieselben Bibliotheken (GNU Radio, libiio,
SoapySDR, gr-m17 in `~/.local`, `rade_c/`, `ft8_lib/`) und verändert an
ihnen nichts.

### 1. Voraussetzungen

```bash
python3 -c "import gnuradio, iio; print('GNU Radio + libiio ok')"
node --version     # >= 18 (für den Frontend-Build)
```

`install-web-trx.sh` prüft alles Weitere selbst (python3-venv, openssl)
und installiert fehlende Systempakete per `apt-get`. Node.js installiert
es bewusst **nicht**: Ist keines oder ein zu altes (< 18) vorhanden, eine
aktuelle LTS-Version z. B. über [nvm](https://github.com/nvm-sh/nvm)
installieren.

### 2. Installieren

Im pluto-tx-Verzeichnis (nicht als root, nicht mit sudo):

```bash
./install-web-trx.sh
```

Das Skript
- legt `web-trx/backend/.venv` **mit** `--system-site-packages` an (GNU
  Radio und libiio kommen aus den Systempaketen und lassen sich nicht per
  pip installieren),
- installiert das Backend (`pip install -e "web-trx/backend[dev]"`),
- baut das Frontend nach `web-trx/frontend/dist/` (`npm ci`, `npm run build`),
- prüft per Import, dass GNU Radio, numpy, libiio und das Backend im venv
  zusammenpassen,
- legt den Starter `~/.local/bin/web-trx` an.

Beliebig oft wiederholbar (z. B. nach einem `git pull`). `web-trx.env`
legt es nicht an.

### 3. Konfiguration (`web-trx/web-trx.env`, optional)

Eine Datei `web-trx/web-trx.env` (wird nicht eingecheckt) kann Einstellungen
setzen, siehe [Konfiguration](#konfiguration):

```bash
cat > web-trx/web-trx.env <<'EOF'
# eigenes Login-Passwort (fehlt es, erzeugt start.sh beim ersten Start
# eines und trägt es hier ein)
WEB_TRX_PASSWORD=bitte-ein-eigenes-passwort
EOF
chmod 600 web-trx/web-trx.env
```

`WEB_TRX_PLUTO_TX_PATH` ist nicht mehr nötig: Standard ist dieses
Repository selbst.

### 4. Starten

```bash
web-trx start        # oder: web-trx/scripts/start.sh
```

oder in `pluto-tx` / `pluto-advanced-rx` in der Zeile „Web-TRX“ auf
**Start** klicken.

### 5. Was beim ersten Start passiert

- Ein selbstsigniertes HTTPS-Zertifikat unter `web-trx/run/tls/`, gültig für localhost, den Hostnamen und alle IPv4-Adressen des Rechners.
- Der Server startet im Hintergrund auf Port 8321 und gibt die Adresse(n) aus, z. B. `https://192.168.178.132:8321`.
- Fehlt das Frontend (`frontend/dist/`), baut `start.sh` es vorher.

Ohne `WEB_TRX_PASSWORD` erzeugt `start.sh` einmalig ein Passwort, gibt es
aus und speichert es in `web-trx/web-trx.env`. Es bleibt dann über Neustarts gleich.

### 6. Im Browser öffnen

1. Die ausgegebene `https://…:8321`-Adresse öffnen. Die Zertifikatswarnung
   einmal bestätigen, das Zertifikat ist selbstsigniert. HTTPS ist nötig,
   weil Browser das Mikrofon nur über HTTPS oder auf `localhost` freigeben.
2. Mit dem Passwort anmelden.
3. **Empfang:** Gerät wählen (z. B. PlutoSDR oder RTL-SDR), optional „Scan“,
   dann „Verbinden“. Der Empfänger startet im gewählten Modus, der
   Wasserfall läuft. Mit „RX-Audio an“ kommt das Audio in den Browser.
4. **Senden:** Gerät verbinden, Frequenz und Modus einstellen, Leistung
   prüfen. PTT gedrückt halten (Maus, Touch oder Leertaste). Beim ersten
   Mal fragt der Browser nach dem Mikrofon. Bei M17 vorher das eigene
   Rufzeichen eintragen.

**Tipps für Digimodes:**
- **RTTY und RADE reagieren empfindlich auf Frequenzablagen zwischen
  Sender und Empfänger.** Liegen sie ein paar Dutzend Hz auseinander,
  kommt bei RTTY nur Zeichensalat an. Die RTTY-AFC gleicht Ablagen unter
  60 Hz absichtlich nicht aus. Abhilfe: „Korr. ppm“ am Gerät setzen. Die
  Ablage zeigt der RADE-Empfang direkt an (ppm = Ablage in Hz ÷ Frequenz
  in MHz, z. B. 58 Hz bei 145 MHz ≈ 0,4 ppm).
- **Waterfall Writer:** Empfänger auf SSB (USB), Bandbreite klein
  (RTL-SDR 0,96 MS/s), FFT 1024, Zoom ×32, Mittelung aus. Sender-Schrift ×3,
  Abstand zum Träger ~1000 Hz. Sehr hoher Zoom macht den Wasserfall
  langsam (eine Zeile braucht FFT-Größe × Zoom Samples), die Schrift wird
  dann zu flach.

Frequenzen: Klick ins Frequenzfeld und `145,15` (MHz) oder `145.150.000`
(Hz) eingeben, oder das Mausrad über einer Ziffer drehen. Klick in den
Wasserfall stellt dort den Empfänger ein, „= RX“ übernimmt die
Empfangsfrequenz für TX.

### 7. Prüfen, ob alles erkannt wird

```bash
web-trx status                               # läuft (PID …), Backend GnuRadioBackend
curl -sk https://localhost:8321/health       # {"status":"ok","backend":"GnuRadioBackend","devices":{…}}
web-trx log                                  # Server-Log (web-trx/run/web-trx.log)
```

Optional ein Hardware-Test, der **nur empfängt** (braucht einen erreichbaren
PlutoSDR und muss bei gestopptem Server laufen, weil beide das Gerät
belegen würden):

```bash
web-trx stop
cd web-trx/backend && WEB_TRX_HW_TESTS=1 .venv/bin/python -m pytest tests/test_radio_backend.py; cd -
web-trx start
```

### 8. Aktualisieren

```bash
web-trx stop
git pull                  # im pluto-tx-Verzeichnis
./install-web-trx.sh      # Abhängigkeiten + Frontend neu bauen
web-trx start
```

### Fehlersuche

| Symptom | Ursache / Abhilfe |
|---|---|
| `start.sh`: „Backend-venv fehlt“ | `./install-web-trx.sh` im pluto-tx-Verzeichnis ausführen. |
| `install-web-trx.sh`: venv ohne `--system-site-packages` | Altes venv beiseitelegen (`mv web-trx/backend/.venv ~/web-trx-venv.alt`), Skript erneut ausführen. |
| `no pluto-tx checkout at …` | `WEB_TRX_PLUTO_TX_PATH` in `web-trx.env` zeigt auf ein falsches Verzeichnis. Zeile entfernen, Standard ist dieses Repo. |
| M17 nicht wählbar („gr-m17 is not importable“) | gr-m17 nicht gebaut (`./install-m17.sh`). Den Server immer über `web-trx start` bzw. `scripts/start.sh` starten, das setzt `LD_LIBRARY_PATH` und die Python-Pfade für `~/.local`. |
| Mikrofon-Fehler beim PTT | Seite über `http://` statt `https://` geöffnet, oder Mikrofon im Browser blockiert. |
| „could not connect to PlutoSDR“ | Pluto nicht erreichbar oder `plutoplus.local` löst falsch auf. Die IP direkt als Verbindung eintragen, z. B. `ip:192.168.2.1`. |
| Gerät belegt | Läuft parallel die pluto-tx-GUI oder `pluto-cli` auf demselben Gerät? Die jeweils andere Anwendung beenden. Die TX-/RX-App warnen selbst, wenn Web-TRX das Gerät offen hat. |
| Wasserfall bleibt leer | Empfänger über „Verbinden“ starten. Das Log (`web-trx log`) zeigt Fehler beim Aufbau des Flowgraphs. |
| App zeigt „Web-TRX: not installed“ | `./install-web-trx.sh` ausführen; der Tooltip nennt, was fehlt. |

## Betrieb

Manuell, oder als systemd-Dienst ([Headless-Betrieb](#headless-betrieb-autostart-als-systemd-dienst-optional)):

```bash
web-trx start              # = scripts/start.sh: startet im Hintergrund, baut das Frontend falls nötig
web-trx stop               # = scripts/stop.sh: sauberes Ende inkl. Sender-Safe-State (unkey, Pluto: max. Dämpfung, LO aus)
web-trx restart            # stop + start (startet nicht neu, wenn der Stopp erzwungen werden musste)
web-trx status             # = scripts/status.sh [--json]
web-trx open               # Seite im Browser öffnen
web-trx log                # Log verfolgen
scripts/start.sh --build   # Frontend neu bauen (nach Änderungen daran)
```

Exit-Codes: `start.sh` 0 gestartet, 3 lief schon, 4 Prozess läuft, aber
noch unbestätigt, 1 Fehler. `stop.sh` 0 gestoppt, 2 musste per SIGKILL
beendet werden (Safe-State des Senders prüfen). `status.sh` 0 läuft,
3 gestoppt, 4 startet/reagiert nicht, 5 nicht installiert, 6 läuft, aber
nicht aus diesem Checkout gestartet. Start und Stopp sind über
`run/start.lock` gegeneinander verriegelt; eine PID-Datei zählt nur, wenn
der Prozess wirklich der Web-TRX-Server ist.

**Aus den Apps:** `pluto-tx` und `pluto-advanced-rx` zeigen in einer Zeile
„Web-TRX“ den Zustand und haben die Knöpfe **Start**, **Stop** und
**Open**. Der Server läuft weiter, wenn die App geschlossen wird. Web-TRX
und eine App dürfen nie gleichzeitig dasselbe SDR öffnen:
- Hat die App beim Klick auf **Start** ein Gerät offen, fragt sie, ob sie
  es vorher trennen soll, und nennt die Geräte, die Web-TRX beim Start
  wieder öffnet.
- Hat Web-TRX einen Gerätetyp offen, warnt die App vor „Connect“ mit
  demselben Typ und verbindet sich beim App-Start nicht automatisch.
- `/health` meldet dafür lokalen Anfragen (127.0.0.1) zusätzlich, welche
  Geräte der Server offen hat.
- `PLUTO_WEBTRX_CONTROL=off` schaltet die Zeile in den Apps ab.

Ein Prozess liefert API, WebSocket und das gebaute Frontend auf Port 8321
aus.

**Ohne Browser läuft nichts weiter:** Schließt der letzte Browser (oder
reißt die Verbindung ab — der Server merkt es per WebSocket-Ping nach
höchstens ~20 s), endet jede Aussendung sofort, auch eine laufende
POCSAG-/RTTY-/Waterfall-Writer-Sendung. Ist nach 10 s
(`WEB_TRX_NO_OPERATOR_S`) kein Browser zurück, werden Empfänger und Sender
getrennt; ein Neuladen der Seite überbrückt das. Nach einem Serverstart
gilt dasselbe, wenn sich 60 s lang kein Browser verbindet.

Nach einem Neustart (auch nach `scripts/stop.sh`/`start.sh`)
verbindet sich eine offene Browserseite von selbst neu, ohne neue
Anmeldung. Der Server öffnet die vorher verbundenen Empfänger/Sender
wieder im zuletzt gewählten Modus; er tastet dabei nie auf, und nach
einem NOTAUS bleibt der Sender aus. Unter `run/` liegen Log (`web-trx.log`), PID, TX-Log
(`web_trx_tx_log.sqlite3`), die Bedien-Einstellungen (`settings.json`:
Frequenzen, Modi, Leistung, Audio-Aufbereitung, Wasserfall) und das
Zertifikat.

### APRS mit Live-Karte und Nachrichten

Der Modus **APRS** startet auf 144,800 MHz, die Frequenz bleibt änderbar, das Audio bleibt wie bei FM verfügbar. Die Karte zeigt
Stationen mit Position, die Tabelle alle Gehörten, daneben stehen die Nachrichten. Es läuft nur über Funk, ohne APRS-IS.

Beim Senden kommen Rufzeichen und Locator aus den Stationsdaten, die Position ist die Mitte des Locators. „Aussenden“ schickt ein
Paket; liegt die letzte Sendung keine 30 s zurück, erscheint ein Hinweis, gesendet wird trotzdem. Die Sendefrequenz ändert sich nicht von selbst; die Taste „144,800 MHz“ stellt sie nur ein.
**Automatisch bestätigen** sendet ohne Klick. Der Schalter ist aus, wird nicht gespeichert, geht mit dem letzten Browser und dem
NOTAUS aus und antwortet nur, wenn der Sender auf APRS und auf der Empfangsfrequenz steht und die letzte Sendung mindestens 30 s zurückliegt. Was nicht rausging, steht im Panel.

Sendeweg und Empfang sind einzeln über Funk geprüft (`docs/aprs/SPEC.md`). Stehen Rechner, Pluto und RTL-SDR nebeneinander, senkt die
Störstrahlung des Rechners die Empfindlichkeit deutlich: Die eigene Testsendung mit −40 dB wurde dann einzeln nicht dekodiert.

### Xiegu X6200

Als Sender wählbar, die Modusliste zeigt dann nur, was er über USB-Audio senden kann (SSB in USB-D und LSB-D, die Digimodes, RADE). Die Leistung steht in Prozent seiner Höchstleistung, Standardgrenze 10 %. Grenzen und Sicherheit: [`docs/x6200/SPEC.md`](../docs/x6200/SPEC.md). Der Empfang über den X6200 ist im Backend eingetragen, im Browser aber noch nicht getestet.

### JTTY

Der Modus **JTTY (WSJT-X)** braucht `install-jtty.sh`, sonst fehlt er in der Auswahl. Unten steht je Nachricht eine Zeile, die mitwächst: UTC, Audiofrequenz, SNR, Text. Nicht abgeschlossene Zeilen sind abgeblendet, `...` steht für verlorene Rahmen. Die Felder **Audio Hz** und **± Hz** legen fest, wo der Decoder sucht; er durchsucht nicht das ganze Band. **Treffpunkt** stellt nur die Frequenz ein, gesendet wird nichts.

Senden: Text bis 80 Zeichen, die Zeile darunter zeigt vorab, was hinausgeht, mit Rahmenzahl und Dauer. **Aussenden** sendet genau einmal und enttastet selbst. NOTAUS bricht ab. Außerhalb der Amateurbänder wird abgewiesen. **Driftausgleich** (Standard an) gleicht die Drift des Pluto während der Sendung aus. Ab 1 GHz sucht der Empfänger mindestens ±500 Hz.

Über Funk geprüft sind Pluto+ und HackRF mit `pluto-cli` und dem Flowgraph; Web-TRX selbst lief nur gegen Fake-Geräte und den Simulator.

### MeshCore

Der Modus **MeshCore (LoRa)** setzt die Frequenz aus dem Preset, sie ist in diesem Modus nicht änderbar; beim Verlassen kommt die alte zurück. Das gilt für Empfang und Senden. Die Karte zeigt Nodes mit Position, die Tabelle alle, die sich per Advert gemeldet haben. Daneben stehen die Nachrichten. Nicht prüfbare Pakete sind ausgeblendet und per Schalter sichtbar.

**Senden** geht nur von Hand: Advert, Kanal-Text oder Direktnachricht, ein Paket je Klick. Die Route steht zunächst auf „Direct“, denn „Flood“ löst fremde Repeater aus. Abgewiesen wird vor dem Tasten, zum Beispiel beim Duty-Cycle-Limit von 10 %, und Text im Amateurfunkband, weil er immer verschlüsselt ist. Standardkanal ist 869,618 MHz im SRD-Band, also kein Amateurfunk. Der Name fällt ohne Eingabe auf das Rufzeichen zurück. Die Position wird nur auf Wunsch gesendet, als Mitte des Locators.

**Kanalschlüssel** bleiben auf dem Server in `meshcore_channels.json` neben den Einstellungen, mit Dateirechten 0600. Der Browser sieht danach nur noch den Namen. Einstellungen, Ereignisse und Sendeprotokoll enthalten den Schlüssel nicht.

**Direktnachrichten** entschlüsseln ist ein Schalter, Standard aus. Die Node-Identität liegt in `~/.config/pluto-tx/meshcore_identity.json` und ist dieselbe wie in den Desktop-Apps; Web-TRX legt sie erst an, wenn der Schalter an ist oder ein Paket gesendet wird. Fremde Direktnachrichten lassen sich nicht lesen. Eine eigene lässt sich erst lesen, wenn das Advert des Absenders gehört wurde.

**Geprüft:** Der Empfang über Funk mit RTL-SDR von einem echten Heltec V3 auf 869,618 MHz: Advert Direct und Flood, ein von einem Repeater weitergeleitetes Advert, Public-Text mit Umlauten; Inhalte und Signaturen stimmten. Mit Fake-Geräten und Simulator laufen Advert und privater Kanal durch den echten Sender und Empfänger.

**Gesendet über Funk:** Pluto+ über Web-TRX, −40 dB, Route Direct: 2 Adverts und 1 Public-Text. Der RTL-SDR las alle 3 mit gültiger Signatur beziehungsweise MAC. Am echten Heltec V3 erschienen in der App der Kontakt und der Text.

**Pluto als Sender und Empfänger zugleich:** Die eigene Sendung über denselben Pluto wurde in 5 von 6 Fällen dekodiert (zuvor 0 von 3). Dafür läuft der Sender in Datenmodi (MeshCore, APRS, POCSAG, RTTY, FT8, JS8, JTTY, Waterfall Writer) ohne seine Audio-Kette, die den Empfang im selben Prozess Samples kostete. Ein Wechsel zwischen Daten- und Sprachmodus baut den Sender deshalb neu auf und dauert einen Moment. In Sprachmodi (FM, SSB, M17, RADE) läuft die Audio-Kette weiter; dort sind ohne Sendung rund 5–15 % Sample-Verlust am Pluto-RX gemessen. Solange RX und TX am Pluto hängen, läuft der Empfänger auf der Rate des Senders, 2,5 MS/s.

**Nicht geprüft:** langer Text, private Kanäle und Direktnachrichten über Funk, Flood-Sendungen, HackRF als Sender, HackRF als Empfänger, das zweite Preset.

### ADS-B mit Live-Karte

Der Modus **ADS-B** empfängt Flugzeuge auf 1090 MHz und zeigt sie wie ADS-L (unten) auf der Karte und in einer Tabelle,
hier mit Rufzeichen, Höhe in Fuß, Knoten, ft/min und Squawk sowie Ringen von 50 bis 400 km. **Nur zur Beobachtung, nicht zur
Navigation oder Kollisionsvermeidung.** Die Sperren sind dieselben: Abstimmung fest auf 1090 MHz, kein Ton, kein
Senden. Er braucht mindestens 2,4 MS/s; ein zu niedriger gespeicherter Wert wird in diesem Modus durch den Standard ersetzt.
Am RTL-SDR gilt dasselbe wie bei ADS-L: Eine unveränderte Verstärkung läuft hier mit 40 dB. Der Server fasst die
Nachrichten zusammen und schickt dem Browser nur einmal pro Sekunde, was sich geändert hat. Details: [`docs/adsb/SPEC.md`](../docs/adsb/SPEC.md).

### ADS-L mit Live-Karte

Der Modus **ADS-L** empfängt die Positionsmeldungen von Luftfahrzeugen auf 868,2 und 868,4 MHz und zeigt sie
im Reiter „Empfang“ auf einer Karte und in einer Tabelle. **Nur zur Beobachtung, nicht zur Navigation oder
Kollisionsvermeidung.** Protokoll und Messwerte: [`docs/adsl/SPEC.md`](../docs/adsl/SPEC.md).

- **Einstellung:** Der Modus stimmt den Empfänger selbst auf 868,85 MHz ab und braucht mindestens 1,8 MS/s; die
  Frequenz ist so lange gesperrt, beim Verlassen kommt die vorige zurück. Ton gibt es hier nicht. Ein RTL-SDR
  mit unveränderter Verstärkung läuft in diesem Modus mit 40 dB statt 20 dB. Der Wasserfall zeigt die beiden
  Kanäle links und rechts seiner Mitte; einzelne Pakete (rund 5 ms) sieht man dort nicht zuverlässig.
- **Karte:** eigene Station und Ringe von 25, 50 und 100 km aus dem Stations-Locator (Locator oben eintragen);
  Pfeil nach Kurs, Spur der letzten Positionen, Notfall rot. Nach 60 s ohne Position wird ein Marker blass, nach
  5 Minuten verschwindet er; in der Tabelle bleibt der Eintrag. Die Höhe gilt über dem WGS-84-Ellipsoid,
  nicht über Meer oder Grund. Luftfahrzeuge ohne Position stehen nur in der Tabelle.
- **Datenfluss:** Die Kartenkacheln lädt der Browser direkt von OpenStreetMap; OSM sieht also die IP des Browsers
  und den Kartenausschnitt. Ohne Internet oder mit `WEB_TRX_MAP_TILE_URL=none` bleiben Marker, Ringe und
  Tabelle voll nutzbar. Die Liste liegt nur im Speicher des Servers, übersteht ein Neuladen der Seite, aber keinen
  Server-Neustart, und wird nirgends gespeichert.
- **Dauerbetrieb:** Ohne offenen Browser trennt der Server den Empfänger wie in jedem Modus nach
  `WEB_TRX_NO_OPERATOR_S`.
- **Stand:** über Funk mit eigenen Testpaketen bis zur Karte geprüft (RTL-SDR, 8 von 8). Echter Flugverkehr ist noch
  nicht empfangen worden, und HackRF ist nur synthetisch getestet.

### Headless-Betrieb (Autostart als systemd-Dienst, optional)

```bash
web-trx/scripts/install-service.sh     # einrichten (ruft sudo selbst auf, nicht mit sudo starten)
web-trx/scripts/uninstall-service.sh   # wieder entfernen
```

`install-service.sh` richtet `/etc/systemd/system/web-trx.service` ein:
Web-TRX startet bei jedem Systemstart, **ohne dass sich jemand anmeldet**,
und nach einem Absturz neu. Der Dienst läuft als der Benutzer, der das
Skript aufruft (nicht als root), und benutzt `start.sh`/`stop.sh`, also
dieselbe PID-Datei und denselben Safe-State beim Stoppen. `web-trx
status`/`log` und die Web-TRX-Zeile in den Apps funktionieren weiter.
Beliebig oft wiederholbar.

- **Ton ohne Anmeldung:** Der TX-Flowgraph öffnet auch für Pluto/HackRF
  immer die Standard-Soundkarte. Vor der Anmeldung läuft kein PipeWire,
  „Connect“ scheitert dann mit `audio_alsa_sink :error: [default]: Host
  is down`. Der Dienst setzt deshalb GNU Radios ALSA-Standardgerät auf
  `null` (`GR_CONF_AUDIO_ALSA_DEFAULT_OUTPUT_DEVICE`/`_INPUT_DEVICE`).
  Das Mikrofon kommt ohnehin aus dem Browser; ausdrücklich gewählte
  Geräte (AIOC `plughw:…`) sind nicht betroffen.
- **Netzwerk ohne Anmeldung:** Das Skript warnt, wenn das aktive WLAN erst
  nach der Anmeldung verbindet (Profil nur für einen Benutzer oder Passwort
  im Schlüsselbund).
- **Bedienung:** `sudo systemctl stop|start|restart web-trx`. `web-trx stop`
  (oder **Stop** in der App) beendet den Server auch, der Dienst startet ihn
  dann aber erst beim nächsten Boot wieder.

`uninstall-service.sh` stoppt den Server (mit Safe-State), schaltet den
Autostart ab und löscht die Unit. `web-trx.env` und `run/` bleiben
erhalten, `web-trx start` funktioniert danach wie vorher.

### Konfiguration

Per Umgebungsvariable oder als `KEY=value`-Zeile in `web-trx.env`:

| Variable | Default | Bedeutung |
|---|---|---|
| `WEB_TRX_PLUTO_TX_PATH` | dieses Repository (Elternordner von `web-trx/`) | pluto-tx-Code, der importiert wird — nur zum Testen gegen einen anderen Checkout nötig |
| `WEB_TRX_PASSWORD` | beim ersten Start erzeugt und in `web-trx.env` gespeichert | Login-Passwort (ein Betreiber, ein geteiltes Passwort) |
| `WEB_TRX_STATION_CALL` / `WEB_TRX_STATION_LOCATOR` | leer | Startwert der Stationsdaten, solange noch keine gespeichert sind |
| `WEB_TRX_NO_OPERATOR_S` | `10` | Sekunden ohne Browser, bis Empfänger und Sender getrennt werden (Aussendungen enden sofort) |
| `WEB_TRX_MAP_TILE_URL` | leer = OpenStreetMap | Kartenbild der ADS-L-Karte: eigene Kachel-URL (`https://…/{z}/{x}/{y}.png`) oder `none` für gar keins |
| `WEB_TRX_SESSIONS_PATH` | `run/sessions.json` | Anmeldungen überleben Server-Neustarts (nur Token-Hashes) |
| `WEB_TRX_HOST` / `WEB_TRX_PORT` | `0.0.0.0` / `8321` | Listen-Adresse |
| `WEB_TRX_TLS` | `true` | `false` = plain HTTP (Mikrofon dann nur auf `localhost`) |
| `WEB_TRX_TX_TIMEOUT_S` | `180` | Sende-Zeitbegrenzung für Sprach-Durchgänge |
| `WEB_TRX_RX_LOCAL_AUDIO` | `false` | Demod-Audio zusätzlich auf dem Server-Lautsprecher |
| `WEB_TRX_BACKEND` | `gnuradio` (in `start.sh`) | `sim` = ohne Hardware (Simulator) |
| `WEB_TRX_TX_LOG_PATH` | `run/web_trx_tx_log.sqlite3` | TX-Aktivitätslog |
| `WEB_TRX_SETTINGS_PATH` | `run/settings.json` | gespeicherte Bedien-Einstellungen |

Für den Zugriff von außerhalb des LANs wird ein VPN (z. B. WireGuard)
empfohlen, statt den Port ins Internet freizugeben.

## Aktueller Stand

Läuft auf echter Hardware (verifiziert mit Pluto+ und RTL-SDR, Details und
Messwerte in [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) Abschnitt 9).

**Fertig**
- `GnuRadioBackend`: pluto-tx-Flowgraphs für Pluto, HackRF und RTL-SDR (RX)
  bzw. Pluto und HackRF (TX). Zusätzlich `SimBackend` für die Entwicklung
  ohne Hardware.
- Empfangs-Verstärkung je Gerät: AGC-Modus und Regler, wie pluto-tx sie
  beschreibt (Pluto: AGC langsam/schnell/hybrid oder manuell; RTL-SDR:
  AGC oder Tuner-Gain; HackRF: LNA, VGA, RF-Amp). Lautstärke im Browser
  (0–200 %, pro Browser gespeichert). Squelch mit Pegelanzeige.
- **FT8-Empfang (experimentell):** Decoder `jt9` (WSJT-X) bzw. ft8_lib neben
  USB; Tabelle je 15-s-Slot mit CQ-Filter und Hervorhebung des eigenen
  Rufzeichens, Slot-Uhr nach Server-Zeit, NTP-Warnung. KW mit dem RTL-SDR
  über Direct Sampling (Q-Zweig, bis 28,8 MHz).
- **FT8-Senden:** Nachricht wählen (CQ, Antwort, Rapport, R+Rapport, RRR,
  RR73, 73, Freitext), Vorschau des gesendeten Texts, Offset, Slot
  (nächster / :00/:30 / :15/:45), bis zu 20 Aussendungen. „Scharf schalten“
  wartet auf den UTC-Slot (Zeitlage macht der Server), ein zweiter Klick,
  NOTAUS, Trennen oder „kein Browser mehr da“ bricht jederzeit ab. Klick auf
  einen Decode übernimmt das Rufzeichen und stellt die Gegenparität ein.
  Jede Aussendung steht mit Text im TX-Log. Im Funkbetrieb noch ungetestet
  (Phase F3 in [`docs/FT8_PLAN.md`](docs/FT8_PLAN.md)).
- **JS8 (JS8Call):** Empfang in Normal/Fast/Turbo/Slow (einzeln oder alle),
  Nachrichten aus mehreren Rahmen zusammengesetzt (Chat nach Gegenstation,
  @ALLCALL), Band-Aktivität und gehörte Stationen; Klick übernimmt das
  Rufzeichen. Senden: CQ, Heartbeat, @ALLCALL- und gerichteter Text, SNR?,
  SNR-Rapport, ACK, Freitext -- gebaut wie JS8Call selbst, Vorschau mit
  Rahmenzahl und Dauer; die Rahmen gehen in aufeinanderfolgenden Perioden
  raus, jeder einzeln getastet. Automatik wie in JS8Call (Autoreply, Heartbeat,
  Relay, Inbox), alles standardmäßig aus, mit Bestätigung, Idle-Watchdog und
  Abschaltung, sobald kein Browser mehr verbunden ist. Im Funkbetrieb getestet
  (2 m, J7/J9). Details: [`docs/JS8.md`](docs/JS8.md).
- Stationsdaten (Rufzeichen, Locator) in der Kopfzeile, gespeichert; genutzt
  für FT8, JS8 und als M17-Vorbelegung. Startwert aus `WEB_TRX_STATION_CALL` /
  `WEB_TRX_STATION_LOCATOR` in `web-trx.env`.
- Empfang: FM (mit De-Emphasis), SSB (USB/LSB), M17 mit Anzeige des
  Rufzeichens, RADE (Sync/SNR), RTTY- und POCSAG-Dekodierung im Panel
  „Empfang“; Demod-Audio im Browser.
- Wasserfall und Spektrum im SDR++-Stil:
  - Klick-zum-Tunen, echtes Zoom-FFT ×1…×64, Mittelung, FFT-Größe.
  - Automatischer Pegel, RX/TX-Marker.
- Senden:
  - FM (Hub 2,5/5 kHz, Pre-Emphasis, CTCSS-Standardtöne), SSB (USB/LSB), M17
    und RADE über das Browser-Mikrofon.
  - Textmodi mit einem Klick: POCSAG, RTTY und Waterfall Writer (Schrift
    im Wasserfall).
  - Audio-Aufbereitung: NF-Pegel, Gate, Kompressor, Limiter, Subton-Pegel.
  - Leistungsregler über den ganzen Bereich des Geräts (Pluto bis 0 dB
    Dämpfung), die Leistung wird je Gerät gespeichert. HackRF: zuschaltbarer
    RF-Verstärker (+14 dB), bei jedem neuen Verbinden aus. Nach einem Server-Neustart wird eine höhere gespeicherte
    Leistung auf −20 dB zurückgesetzt, eine niedrigere bleibt erhalten.
- Sicherheit: NOTAUS, automatisches Abtasten wenn das Mikrofon-Audio
  abreißt, Sende-Zeitbegrenzung, persistentes TX-Log.
- Login mit geteiltem Passwort, HTTPS, Bedien-Einstellungen bleiben über
  Neustarts erhalten, manueller Betrieb über `web-trx` bzw.
  `scripts/start.sh`/`stop.sh` und aus der TX-/RX-App, optional als
  systemd-Dienst für Headless-Betrieb (`scripts/install-service.sh`).
- Modus-Parameter zentral geprüft in `backend/web_trx/modes.py`.

**Offen**
- Latenzmessung über einen echten VPN-Link, Opus über langsame Links.
- Mehrgeräte-Betrieb, TLS-Reverse-Proxy.
- FT8-Senden im Funkbetrieb testen (F3/F4 in
  [`docs/FT8_PLAN.md`](docs/FT8_PLAN.md)).
- JS8 mit echten Gegenstationen auf KW (2 m über Luft ist getestet, J7).
- Weitere Modi (PSK31, FreeDV, Meshtastic, File Broadcast, …).

## Struktur

```
backend/    FastAPI-Server (Python) -- SessionManager, SimBackend, GnuRadioBackend, Auth, TX-Log, Modus-Parameter
frontend/   Svelte/TypeScript-SPA -- Login, Wasserfall, RX/TX-Panels, TX-Verlauf, Event-Log
scripts/    start.sh / stop.sh / status.sh (manueller Betrieb, auch von den Apps aufgerufen)
docs/       Projektplan, Debugging-Strategie, FT8-Plan, JS8
run/        Laufzeitdaten (nicht eingecheckt)
```

Außerhalb dieses Ordners gehören dazu: `../install-web-trx.sh`,
`../pluto_tx/webtrx_control.py` (Start/Stopp/Status für Apps und
Kommandozeile), `../pluto_tx/webtrx_widget.py` (die Zeile in den Apps)
und `../.github/workflows/web-trx.yml` (CI).

## Entwicklung (ohne Hardware)

Tests und Linter für Web-TRX laufen **aus `web-trx/backend`** mit pytest.
Die pluto-tx-Tests laufen dagegen aus der Repo-Wurzel mit
`python3 -m unittest discover tests`. Beide Ordner heißen `tests`, deshalb
nie pytest aus der Repo-Wurzel starten.

```bash
cd web-trx/backend
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e ".[dev]"   # macht sonst install-web-trx.sh
.venv/bin/python -m pytest            # Tabellen-Tests prüfen gegen ../../pluto_tx/config.py
.venv/bin/ruff check web_trx tests
.venv/bin/uvicorn web_trx.server:create_app --factory --reload --port 8321   # SimBackend
```

```bash
cd web-trx/frontend
npm ci
npm run dev      # Vite mit Proxy auf das Backend (127.0.0.1:8321), siehe vite.config.ts
npm run check    # Typecheck
npm run build
```

`WEB_TRX_BACKEND` wählt die `SessionBackend`-Implementierung: `sim`
(Default bei direktem uvicorn-Start, keine Hardware nötig) oder `gnuradio`.

## Herkunft

Web-TRX entstand als eigenes Repository
[jochenhammes/Web-TRX](https://github.com/jochenhammes/Web-TRX) (dort mit
voller Einzelhistorie archiviert) und band pluto-tx als Submodule ein. Seit
der Zusammenführung liegt es hier; der Import-Commit nennt den
übernommenen Stand.

## Lizenz

[GPLv3](../LICENSE) — wie das ganze pluto-tx-Repository.
