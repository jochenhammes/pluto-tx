# Web-TRX

Einheitliche Weboberfläche für [`pluto-tx`/`pluto-advanced-rx`](https://github.com/jochenhammes/pluto-tx)
(Sende-/Empfangs-Software für ADALM-PLUTO, HackRF One und RTL-SDR). Ein
lizenzierter Funkamateur bedient die am Server angeschlossenen SDRs
vollständig über den Browser — Senden und Empfangen, inklusive Wasserfall/
Spektrum, Audio und Digimodes.

**Stand:**
- **Empfang:** FM, SSB (USB/LSB) und M17 mit Audio im Browser. POCSAG wird dekodiert.
- **Wasserfall:** echtes Zoom-FFT, Mittelung, automatischer Pegel.
- **Senden:** FM, SSB (USB/LSB) und M17 über das Browser-Mikrofon, mit Audio-Aufbereitung und Leistungsregler.
- **POCSAG senden:** eine Nachricht an eine RIC.
- **Sicherheit:** NOTAUS, automatisches Abtasten wenn das Mikrofon-Audio abreißt, Sende-Zeitbegrenzung, TX-Log.

Projektplan (Architektur, Betriebsarten, Meilensteine, aktueller Stand):
[`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md).
Debug-/Teststrategie und bekannte Stolpersteine: [`docs/DEBUGGING.md`](docs/DEBUGGING.md).

## Installation (pluto-tx ist bereits installiert)

Diese Anleitung geht davon aus, dass auf dem Rechner mit den SDRs
`pluto-tx` schon per `install.sh` eingerichtet ist (GNU Radio, libiio,
gr-osmosdr/SoapySDR aus den Systempaketen). Optional sind auch gr-m17
(`install-m17.sh`, landet in `~/.local`) und RADE (`install-rade.sh`)
gebaut. Kurz: `pluto-tx` bzw. `pluto-advanced-rx` starten und funktionieren.
Web-TRX installiert **nichts** an pluto-tx nach und verändert es nicht; es
importiert dessen Python-Code direkt aus der vorhandenen Installation.

### 1. Voraussetzungen prüfen

```bash
python3 -c "import gnuradio, iio; print('GNU Radio + libiio ok')"
python3 -m venv --help >/dev/null && echo "venv ok"
node --version     # >= 18 (für den Frontend-Build)
npm --version
openssl version    # für das selbstsignierte HTTPS-Zertifikat
```

Fehlt etwas davon, auf Debian/Ubuntu nachinstallieren:

```bash
sudo apt install python3-venv nodejs npm openssl
```

Liefert die Distribution ein Node.js älter als 18, eine aktuelle LTS-Version
z. B. über [nvm](https://github.com/nvm-sh/nvm) installieren.

### 2. Repository klonen

```bash
cd ~/Dokumente
git clone --recurse-submodules https://github.com/jochenhammes/Web-TRX.git
cd Web-TRX
```

(`vendor/pluto-tx` ist ein gepinntes Submodule, das nur als Fallback dient,
wenn keine eigene pluto-tx-Installation angegeben ist, siehe Schritt 4.)

### 3. Python-Umgebung für das Backend

GNU Radio und libiio lassen sich nicht per pip installieren, sie kommen aus
den Systempaketen. Das venv muss sie daher sehen, also unbedingt **mit**
`--system-site-packages` anlegen:

```bash
python3 -m venv --system-site-packages backend/.venv
backend/.venv/bin/pip install -e "backend[dev]"
```

### 4. Konfiguration (`web-trx.env`)

Im Repo-Root eine Datei `web-trx.env` anlegen (wird nicht eingecheckt):

```bash
cat > web-trx.env <<'EOF'
# Pfad zur vorhandenen pluto-tx-Installation (das Verzeichnis, das
# pluto_tx/ und pluto_advanced_rx/ enthält und in dem install.sh lief)
WEB_TRX_PLUTO_TX_PATH=/home/<user>/Dokumente/plutosdr
# eigenes Login-Passwort (fehlt es, erzeugt start.sh beim ersten Start
# eines und trägt es hier ein)
WEB_TRX_PASSWORD=bitte-ein-eigenes-passwort
EOF
chmod 600 web-trx.env
```

Der Default für `WEB_TRX_PLUTO_TX_PATH` ist `~/Dokumente/plutosdr`. Liegt
pluto-tx genau dort, kann die Zeile entfallen. Alle Einstellungen stehen
unter [Konfiguration](#konfiguration).

### 5. Erster Start

```bash
scripts/start.sh
```

Beim ersten Start passiert automatisch:
- `npm install` und der Frontend-Build nach `frontend/dist/`.
- Ein selbstsigniertes HTTPS-Zertifikat unter `run/tls/`, gültig für localhost, den Hostnamen und alle IPv4-Adressen des Rechners.
- Der Server startet im Hintergrund auf Port 8321 und gibt die Adresse(n) aus, z. B. `https://192.168.178.132:8321`.

Ohne `WEB_TRX_PASSWORD` erzeugt `start.sh` einmalig ein Passwort, gibt es
aus und speichert es in `web-trx.env`. Es bleibt dann über Neustarts gleich.

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
curl -sk https://localhost:8321/health      # {"status":"ok","backend":"GnuRadioBackend"}
tail -f run/web-trx.log                     # Server-Log
```

Optional ein Hardware-Test, der **nur empfängt** (braucht einen erreichbaren
PlutoSDR und muss bei gestopptem Server laufen, weil beide das Gerät
belegen würden):

```bash
scripts/stop.sh
WEB_TRX_PLUTO_TX_PATH=~/Dokumente/plutosdr WEB_TRX_HW_TESTS=1 \
  backend/.venv/bin/python -m pytest backend/tests/test_radio_backend.py
scripts/start.sh
```

### 8. Aktualisieren

```bash
scripts/stop.sh
git pull --recurse-submodules
backend/.venv/bin/pip install -e "backend[dev]"   # falls sich Abhängigkeiten geändert haben
scripts/start.sh --build                          # Frontend neu bauen
```

### Fehlersuche

| Symptom | Ursache / Abhilfe |
|---|---|
| `start.sh`: „Backend-venv fehlt“ | Schritt 3 ausführen. |
| „Start fehlgeschlagen“, Log leer oder Import-Fehler zu `gnuradio` | venv ohne `--system-site-packages` angelegt: `rm -rf backend/.venv` und Schritt 3 wiederholen. |
| `no pluto-tx checkout at …` | `WEB_TRX_PLUTO_TX_PATH` in `web-trx.env` zeigt nicht auf das pluto-tx-Verzeichnis. |
| M17 nicht wählbar („gr-m17 is not importable“) | gr-m17 nicht gebaut (`install-m17.sh` von pluto-tx). Den Server immer über `scripts/start.sh` starten, das setzt `LD_LIBRARY_PATH` und die Python-Pfade für `~/.local`. |
| Mikrofon-Fehler beim PTT | Seite über `http://` statt `https://` geöffnet, oder Mikrofon im Browser blockiert. |
| „could not connect to PlutoSDR“ | Pluto nicht erreichbar oder `plutoplus.local` löst falsch auf. Die IP direkt als Verbindung eintragen, z. B. `ip:192.168.2.1`. |
| Gerät belegt | Läuft parallel die pluto-tx-GUI oder `pluto-cli` auf demselben Gerät? Die jeweils andere Anwendung beenden. |
| Wasserfall bleibt leer | Empfänger über „Verbinden“ starten. Das Log (`run/web-trx.log`) zeigt Fehler beim Aufbau des Flowgraphs. |

## Betrieb

Bewusst manuell, (noch) kein systemd-Dienst:

```bash
scripts/start.sh           # startet im Hintergrund, baut das Frontend falls nötig
scripts/start.sh --build   # Frontend neu bauen (nach Änderungen daran)
scripts/stop.sh            # sauberes Ende inkl. Sender-Safe-State (unkey, Pluto: max. Dämpfung, LO aus)
```

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

### Konfiguration

Per Umgebungsvariable oder als `KEY=value`-Zeile in `web-trx.env`:

| Variable | Default | Bedeutung |
|---|---|---|
| `WEB_TRX_PLUTO_TX_PATH` | `~/Dokumente/plutosdr` | pluto-tx-Installation, deren Code importiert wird |
| `WEB_TRX_PASSWORD` | beim ersten Start erzeugt und in `web-trx.env` gespeichert | Login-Passwort (ein Betreiber, ein geteiltes Passwort) |
| `WEB_TRX_STATION_CALL` / `WEB_TRX_STATION_LOCATOR` | leer | Startwert der Stationsdaten, solange noch keine gespeichert sind |
| `WEB_TRX_NO_OPERATOR_S` | `10` | Sekunden ohne Browser, bis Empfänger und Sender getrennt werden (Aussendungen enden sofort) |
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
- Stationsdaten (Rufzeichen, Locator) in der Kopfzeile, gespeichert; genutzt
  für FT8 und als M17-Vorbelegung. Startwert aus `WEB_TRX_STATION_CALL` /
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
  Neustarts erhalten, manueller Betrieb über `scripts/start.sh`/`stop.sh`.
- Modus-Parameter zentral geprüft in `backend/web_trx/modes.py`.

**Offen**
- Latenzmessung über einen echten VPN-Link, Opus über langsame Links.
- Mehrgeräte-Betrieb, systemd-Dienst, TLS-Reverse-Proxy.
- FT8 (in pluto-tx vorhanden, dort noch ungetestet im Funkbetrieb; Plan in
  [`docs/FT8_PLAN.md`](docs/FT8_PLAN.md)).
- Weitere Modi (PSK31, FreeDV, Meshtastic, MeshCore, File Broadcast, …).

## Struktur

```
backend/    FastAPI-Server (Python) -- SessionManager, SimBackend, GnuRadioBackend, Auth, TX-Log, Modus-Parameter
frontend/   Svelte/TypeScript-SPA -- Login, Wasserfall, RX/TX-Panels, TX-Verlauf, Event-Log
scripts/    start.sh / stop.sh (manueller Betrieb)
vendor/     Git-Submodule: pluto-tx (read-only, gepinnter Commit, Fallback)
docs/       Projektplan, Debugging-Strategie
run/        Laufzeitdaten (nicht eingecheckt)
```

## Entwicklung (ohne Hardware)

```bash
python3 -m venv backend/.venv && backend/.venv/bin/pip install -e "backend[dev]"
backend/.venv/bin/python -m pytest backend    # läuft komplett ohne GNU Radio/Hardware
cd backend && .venv/bin/uvicorn web_trx.server:create_app --factory --reload --port 8321   # SimBackend
```

```bash
cd frontend
npm install
npm run dev      # Vite mit Proxy auf das Backend (127.0.0.1:8321), siehe vite.config.ts
npm run check    # Typecheck
npm run build
```

`WEB_TRX_BACKEND` wählt die `SessionBackend`-Implementierung: `sim`
(Default bei direktem uvicorn-Start, keine Hardware nötig) oder `gnuradio`.

## Submodule

`vendor/pluto-tx` ist ein Git-Submodule auf einem fest gepinnten Commit —
**read-only**, nie von hier aus verändert. Ein Versions-Bump ist ein
bewusster Einzelschritt:

```bash
git -C vendor/pluto-tx fetch
git -C vendor/pluto-tx checkout <neuer-commit>
git add vendor/pluto-tx
git commit -m "vendor/pluto-tx: bump to <neuer-commit>"
```

Beim Klonen: `git clone --recurse-submodules ...` bzw. nachträglich
`git submodule update --init`. Nach jedem `git pull` zusätzlich
`git submodule update`, sonst bleibt `vendor/pluto-tx` auf dem alten Stand.

## Lizenz

[GPLv3](LICENSE) — wie `pluto-tx`, dessen Code das Backend importiert.
