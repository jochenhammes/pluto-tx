# FT8 in Web-TRX — Integrationsplan

Stand der Analyse: pluto-tx `48f55dd` (FT8 für TX, RX und CLI, dort als
*„UNGETESTET im echten Funkbetrieb“* markiert), Web-TRX `129ffc0`.

Dieser Plan ist so geschrieben, dass eine Claude-Code-Session auf dem
Radio-Server ihn Phase für Phase ausführen kann. Abschnitt 0 ist die
Arbeitsanleitung dafür, die Abschnitte 1–4 begründen das Design, Abschnitt 5
enthält die Schritte.

## 0. Arbeitsanleitung für die ausführende Session

**Zuerst lesen:** `README.md`, `docs/PROJECT_PLAN.md` (v. a. Abschnitt 9),
`docs/DEBUGGING.md` (bekannte Stolpersteine), dann diesen Plan komplett. Für
die pluto-tx-Seite: `pluto_cli/runtime.py` (`_run_ft8_series`),
`pluto_cli/tx.py` (`run_ft8`), `pluto_cli/rx.py` (`on_ft8_decodes`),
`pluto_tx/ft8.py`, `pluto_advanced_rx/ft8_rx.py` in der Repo-Wurzel (Web-TRX
liegt seit der Zusammenführung als `web-trx/` im pluto-tx-Repository).

**Feste Regeln:**
- **Das ganze Repository ist frei bearbeitbar** (seit der Zusammenführung
  gilt die frühere Regel „pluto-tx read-only“ nicht mehr). Änderungen an
  `pluto_tx/`, `pluto_advanced_rx/` oder `pluto_cli/` dürfen die Apps nicht
  brechen: vor jedem Commit laufen dafür zusätzlich die pluto-tx-Tests
  (`QT_QPA_PLATFORM=offscreen PLUTO_WEBTRX_CONTROL=off python3 -m unittest
  discover tests` in der Repo-Wurzel).
- **Gearbeitet wird direkt auf `main`** im pluto-tx-Repo. Kleine Commits je
  abgeschlossenem Schritt, Tests vorher grün, Push am Ende jeder Phase.
- **FT8 selbst wird nicht nachgebaut** (keine eigene Synthese, kein eigener
  Decoder, keine eigene Slot-Planung außer als Test-Double im SimBackend).
- **Senden über Antenne nur nach ausdrücklicher Freigabe des Betreibers**,
  mit Frequenz und Leistung, die er nennt. Tests ohne Freigabe nur im
  SimBackend. Nie unbeaufsichtigt senden.
- **Haltepunkte:** am Ende von F1, vor Beginn von F3 und vor Beginn von F4
  anhalten, Stand berichten, Freigabe abwarten. Ebenso anhalten, wenn eine
  Annahme dieses Plans nicht stimmt (z. B. eine pluto-tx-Schnittstelle
  anders aussieht).
- Neue Stolpersteine in `docs/DEBUGGING.md` festhalten, den Stand am Ende in
  `README.md` („Aktueller Stand“) und `docs/PROJECT_PLAN.md` Abschnitt 9
  nachtragen.

**Befehle:**
```bash
backend/.venv/bin/ruff check backend/web_trx backend/tests
backend/.venv/bin/python -m pytest backend          # ohne Hardware
(cd frontend && npm run check && npm run build)
scripts/stop.sh; scripts/start.sh --build            # Server mit neuem Frontend
tail -f run/web-trx.log
# Hardware-Tests: Server vorher stoppen (beide würden das Gerät belegen)
scripts/stop.sh
WEB_TRX_HW_TESTS=1 backend/.venv/bin/python -m pytest backend/tests/test_radio_backend.py
```
Browser-Tests laufen gegen `WEB_TRX_BACKEND=sim` (siehe `docs/DEBUGGING.md`).

## 1. Ziel und Rahmen

FT8 aus pluto-tx wird in Web-TRX so angebunden wie die übrigen Modi: pluto-tx
liefert Signalverarbeitung, Nachrichtenkodierung, Slot-Planung und Decoder,
Web-TRX nur Sitzung, Protokoll und Oberfläche.

Übernommen wird auch die Bedienphilosophie von pluto-tx: **manuelle
QSO-Führung**. Der Betreiber wählt pro 15-s-Slot die Nachricht selbst (CQ,
Antwort, Rapport, R+Rapport, RRR, RR73, 73, Freitext). Einen automatischen
QSO-Ablauf wie in WSJT-X hat pluto-tx bewusst nicht, Web-TRX auch nicht.

## 2. Was pluto-tx liefert

| Bereich | Schnittstelle | Wo |
|---|---|---|
| TX-Modus | `PlutoTxFlowgraph.MODE_FT8`, Live-Umschaltung per `set_mode()` wie die anderen Modi; `FT8_AVAILABLE` | `pluto_tx/flowgraph.py` |
| TX-Nachricht | `set_ft8_text()`, `set_ft8_tone_hz()` (Audio-Offset 200–2900 Hz, Default 1500) | dto. |
| TX-Vorabprüfung | `ft8_problem()` → `None` oder Grund (Text nicht kodierbar, Offset außerhalb, ft8_lib fehlt); `prepare_ft8()` synthetisiert vorab | dto. |
| TX-Zeitlage | `ft8_start_at` (Epoch-Sekunde des Signalbeginns) vor `key_ptt()` setzen; der Flowgraph sendet bis dahin Stille (`Ft8TimedSource`), `ft8_hold_s` = Haltezeit ab Auftasten | `pluto_tx/ft8_source.py` |
| Slot-Planung | `ft8.plan_transmission(now, parity, key_early_s, start_in_slot_s, late_max_s)` → `(key_at, start_at)`; `slot_parity()`, `current_slot_start()` | `pluto_tx/ft8.py` |
| Nachrichten | `ft8.MESSAGE_KINDS`, `ft8.compose(kind, my_call, my_locator, dx_call, report_db, free_text)`, `FREE_TEXT_MAX = 13` | dto. |
| Driftkompensation | `ft8_drift_comp_enabled` (Default an), gemessenes Modell für Pluto in `config.FT8_DRIFT_MODEL` | `pluto_tx/config.py` |
| RX | `AdvancedRxFlowgraph(active_digimode="ft8", on_ft8_decodes=cb, ft8_backend=..., ft8_my_call=..., ft8_my_grid=...)` als paralleler Zweig neben USB | `pluto_advanced_rx/flowgraph.py` |
| RX-Ergebnis | `cb(slot_start, [Ft8Decode(text, snr_db, dt_s, freq_hz), …])` einmal pro Slot, ~14,8 s nach Slotbeginn, **aus dem Decoder-Thread**; `tb.ft8_receiver.last_error`, `.slots_decoded` | `pluto_advanced_rx/ft8_rx.py` |
| RX-Hilfen | `sender_call(text)` (Absender-Rufzeichen), `available_backends()` (`jt9`, `ft8lib`) | `pluto_advanced_rx/ft8_decoder.py` |
| Direct Sampling | `AdvancedRxFlowgraph(direct_sampling=...)`, `set_direct_sampling()` (RTL-SDR, HF bis 28,8 MHz) | `pluto_advanced_rx/flowgraph.py` |
| Uhr | `timedatectl show -p NTPSynchronized --value` (so prüfen es beide pluto-tx-GUIs) | `pluto_tx/gui.py` `_ntp_synchronized()` |
| Installation | `install-ft8.sh`: baut ft8_lib (Encoder, Ersatz-Decoder), installiert `wsjtx` (Decoder `jt9`) | pluto-tx |
| Blaupause | `pluto-cli tx ft8` / `rx --digimode ft8` | `pluto_cli/` |

`pluto_tx/ft8.py` braucht nur numpy und ctypes, kein GNU Radio und auch kein
gebautes ft8_lib (dann ist nur `FT8_AVAILABLE` falsch). Geprüft: Die
Slot-Planung lässt sich auch ohne GNU Radio importieren und testen.

## 3. Was FT8 von den bisherigen Modi unterscheidet

1. **Zeitgesteuertes Senden.** Zwischen PTT und Signalbeginn liegen bis zu
   14 s (nächster Slot) bzw. 29 s (feste Parität), nachgerechnet mit
   `plan_transmission`: Die Aussendung wartet auf den passenden UTC-Slot, wird
   3 s vorher aufgetastet (der Flowgraph-Umbau dauert beim Pluto 0,5–2 s)
   und beginnt 0,5 s nach Slotbeginn. Web-TRX braucht dafür einen neuen
   Zustand **„scharf“** zwischen „bereit“ und „sendet“, der jederzeit
   abbrechbar ist, plus Wiederholungen alle 30 s in derselben Slot-Parität.
2. **Die Uhr des Servers zählt.** Slot-Timing passiert komplett auf dem
   Server, Netzwerk- und VPN-Latenz spielen für die Zeitlage **keine** Rolle.
   Dafür muss die Server-Uhr per NTP auf ±1 s stimmen. Der Browser zeigt nur
   an; seine eigene Uhr darf abweichen.
3. **Ergebnisse pro Slot statt Zeichenstrom.** Auf einem belebten Band
   liefert ein Slot Dutzende Decodes auf einmal. Sie gehen gebündelt als ein
   Event pro Slot an den Browser.
4. **Senden ohne Mikrofon, aber in Serien.** Der Mikrofon-Watchdog der
   Sprachmodi greift nicht. Eine CQ-Serie würde ohne Gegenmaßnahme
   weitersenden, auch wenn niemand mehr zusieht (siehe 4.3, „Bediener weg“).

## 4. Design

### 4.1 Protokoll

Bestehende Requests bekommen bei FT8 eine erweiterte Bedeutung:

| Request | Bedeutung bei FT8 |
|---|---|
| `select_mode` tx `ft8` | Nachricht und Sende-Optionen setzen (siehe 4.2) |
| `ptt_on` | Serie **scharf schalten** (nicht sofort senden) |
| `ptt_off` | Scharfe Serie abbrechen; eine laufende Aussendung wird beendet |
| `estop` | wie bisher, bricht zusätzlich jede scharfe Serie ab |
| `select_mode` rx `ft8` | Empfänger mit FT8-Dekoder neben USB |

Neuer Request `set_station` `{call, locator}` (siehe 4.2).

Neue Events:

| Event | Felder | Wann |
|---|---|---|
| `ft8_armed` | `slot_utc`, `key_in_s`, `start_at` (Server-Epoch), `repetition`, `of` | Serie scharf, vor jeder Aussendung |
| `ft8_cancelled` | `reason` (`ptt_off`, `estop`, `disconnect`, `mode_change`, `no_operator`) | Serie abgebrochen, bevor alle Aussendungen gelaufen sind |
| `ft8_slot` | `slot_start`, `utc`, `decodes: [{snr_db, dt_s, freq_hz, text, sender}]` | einmal pro dekodiertem Slot |
| `ft8_status` | `decoder`, `slots_decoded`, `last_error` | periodisch, wenn RX in FT8 läuft |
| `station` | `call`, `locator` | nach `set_station` |

`keyed`/`unkeyed`/`tx_text` werden wie bei den anderen Einmal-Modi pro
Aussendung gesendet. Damit landet jede Aussendung mit Nachrichtentext im
TX-Log. `hello` bekommt `server_time` (für die Slot-Uhr), `station` und
`features.ft8 = {tx, rx_backends, clock_synced}`.

### 4.2 Parameter und Stationsdaten

**Stationsdaten (entschieden):** eigene Betreiber-Einstellung, nicht Teil
eines Modus.
- Werte dieser Station: **Rufzeichen `DA2JH`, Locator `JO43`**.
- Request `set_station {call, locator}`. Geprüft in einem neuen, reinen
  Modul `web_trx/station.py`:
  - Rufzeichen: 3–10 Zeichen aus A–Z, 0–9 und `/`, mit mindestens einer
    Ziffer und einem Buchstaben, in Großbuchstaben.
  - Locator: Maidenhead mit 4 oder 6 Stellen (`[A-R]{2}[0-9]{2}([A-X]{2})?`).
- Gespeichert in `run/settings.json` unter `"station"` (neben `rx`/`tx`),
  übertragen im `hello` und als `station`-Event.
- Startwert aus `WEB_TRX_STATION_CALL` / `WEB_TRX_STATION_LOCATOR`, gilt
  nur, solange noch nichts gespeichert ist. Auf dem Radio-Server kommen
  beide Werte in `web-trx.env` (nicht eingecheckt). Im Code bleiben die
  Defaults leer, damit keine persönlichen Daten als Voreinstellung im
  Quelltext stehen.
- Benutzt von FT8-TX (Nachricht), FT8-RX (`ft8_my_call`/`ft8_my_grid` für
  `jt9`) und als Vorbelegung des M17-Quellrufzeichens, wenn dieses leer ist.
- Oberfläche: kleines Feld „Station“ in der Kopfzeile oder als eigener
  Einstellungsdialog, mit sofortiger Prüfung.

**FT8-Modusparameter (`web_trx/modes.py`):**
- **TX `ft8`:**

  | Parameter | Werte |
  |---|---|
  | `kind` | einer der `MESSAGE_KINDS` |
  | `dx_call` | Rufzeichen der Gegenstation |
  | `report_db` | −30…+30 |
  | `free_text` | ≤ 13 Zeichen |
  | `offset_hz` | 200–2900, Default 1500 |
  | `slot` | `any` / `even` / `odd` |
  | `drift_comp` | Default an |
  | `repeat_count` | 1…`FT8_MAX_REPEATS` (siehe 4.3) |

- **RX `ft8`:** `decoder` (`auto`/`jt9`/`ft8lib`, Default `auto`).
- Den Nachrichtentext bildet das Backend mit `ft8.compose()` aus
  Stationsdaten und Parametern und meldet ihn im `mode`-Event zurück. Das
  Frontend zeigt ihn nur als Vorschau.
- `modes.py` prüft nur die Struktur. Ob sich der Text als FT8 kodieren
  lässt, weiß nur ft8_lib: `GnuRadioBackend` prüft das schon bei
  `select_mode` per `tb.ft8_problem()`.
- Wertetabellen (`FT8_TONE_RANGE_HZ`, `FT8_DEFAULT_TONE_HZ`,
  `MESSAGE_KINDS`, `FREE_TEXT_MAX`) werden wie bei FM/RTTY gespiegelt und
  per Test gegen die pluto-tx-Dateien im selben Repository geprüft.
  - `MESSAGE_KINDS` und `FREE_TEXT_MAX` stehen in `pluto_tx/ft8.py`. Das
    Modul hat relative Imports, der Test muss es also als Paket importieren
    (Repo-Wurzel auf `sys.path`) statt per Dateipfad.

### 4.3 Backend: TX

**Neues Modul `web_trx/ft8_series.py`** (ohne GNU Radio, voll testbar): der
Ablauf einer Sendeserie als kleine Zustandsmaschine
`scharf → sendet → Pause → scharf …`, mit injizierter Uhr, injizierter
Slot-Planung und injizierten `key`/`unkey`-Funktionen. Es kapselt:
- die Planung jeder Aussendung und Wiederholungen alle 30 s in der Parität
  der ersten (wie `pluto-cli`),
- den Abbruch jederzeit (PTT, NOTAUS, Trennen, Moduswechsel, Bediener weg),
- die Events `ft8_armed` und `ft8_cancelled`.

`GnuRadioBackend` nutzt es mit `pluto_tx.ft8.plan_transmission` und der
echten Uhr, `SimBackend` mit derselben Serienlogik. Der Planer des
SimBackend ist ein Test-Double, das ein Test gegen pluto-tx' Planer
abgleicht.

**Bediener weg** (Standard, siehe 8): ~~Eine scharfe Serie bricht ab, wenn
30 s lang kein Browser verbunden ist (`FT8_NO_OPERATOR_S = 30`).~~
Inzwischen strenger und für alle Modi gelöst: Der „Kein Bediener“-Watchdog
im `SessionManager` beendet jede Aussendung sofort, wenn der letzte Browser
geht, und trennt die Geräte nach 10 s (28.09.2026). Für FT8 heißt das: Eine
scharfe Serie wird sofort abgebrochen (`ft8_cancelled`, `no_operator`);
`ptt(False)` muss dafür auch eine scharfe Serie abbrechen.
Wiederholungen sind auf `FT8_MAX_REPEATS = 20` begrenzt, das sind 10 min
CQ. `SessionManager` muss dem Backend dafür melden, wie viele Clients
verbunden sind.

Änderungen in `radio_backend.py`:
- `TX_MODES["ft8"] = PlutoTxFlowgraph.MODE_FT8`, verfügbar nur mit
  `FT8_AVAILABLE`.
- `_apply_tx_params`: Text per `compose()` aus Stationsdaten und Parametern,
  dann `set_ft8_text()`, `set_ft8_tone_hz()` und `ft8_drift_comp_enabled`.
- `_tx_problem`: `tb.ft8_problem()`.
- `_key()`: bei `ft8` statt sofortigem Auftasten die Serie starten. Pro
  Aussendung:
  1. `prepare_ft8()` im Thread,
  2. warten bis `key_at`,
  3. `tb.ft8_start_at = start_at` setzen, `key_ptt()`,
  4. `keyed` und `tx_text` senden,
  5. Abtasten nach `tb.ft8_hold_s` plus kurzem Nachlauf.
- `estop()`, `disconnect` und Moduswechsel brechen eine scharfe Serie ab.
- `ft8` in `ONE_SHOT_TX_MODES` aufnehmen, damit Mikrofon-Watchdog und
  Mikrofon-Statistik nicht greifen.

Die Genauigkeit hängt nicht an `asyncio`: Aufgetastet wird 3 s früher, den
exakten Signalbeginn stellt pluto-tx' `Ft8TimedSource` her (dort gemessen:
±8 ms). Die Driftkompensation rechnet mit der Zeit seit dem Start des
Flowgraphs. Web-TRX startet den TX-Flowgraph beim Verbinden und lässt ihn
laufen, das passt zum Modell.

### 4.4 Backend: RX

- `RX_DEMOD_MODES["ft8"] = MODE_SSB`, `RX_DIGIMODES["ft8"] = "ft8"` —
  genau das RTTY-Muster: Digimode-Zweig neben USB, die Töne bleiben hörbar.
- `_build_rx` übergibt `on_ft8_decodes`, `ft8_backend` sowie
  `ft8_my_call`/`ft8_my_grid` aus den Stationsdaten. Der Callback kommt aus
  dem Decoder-Thread und geht über `_from_gr_thread` als `ft8_slot` an alle
  Browser, je Decode mit `sender` aus `sender_call()`.
- `_rx_aux_loop` meldet `ft8_status` aus `tb.ft8_receiver.last_error` und
  `.slots_decoded`.
- Verfügbar nur, wenn `available_backends()` nicht leer ist.
- Der Squelch betrifft FT8 nicht: Die Dekoder greifen vor ihm ab, wie heute
  schon RTTY und POCSAG.
- **Direct Sampling für RTL-SDR** durchreichen, damit HF empfangbar wird
  (siehe Tabelle in 2):
  - neuer RX-Gain-Name `direct_sampling` (`off`/`i`/`q`), nur für RTL-SDR,
  - gespeichert wie die übrigen RX-Einstellungen, bei Änderung Rebuild.

### 4.5 Uhr

Wie pluto-tx' GUIs: `timedatectl show -p NTPSynchronized --value` beim Start
und danach alle 5 min. Das Ergebnis geht als `features.ft8.clock_synced`
raus bzw. als Warn-Banner im FT8-Modus. Kann es nicht ermittelt werden, gilt
es wie in pluto-tx als synchron. Die Slot-Uhr im Browser rechnet mit dem
Versatz zwischen `server_time` und Browser-Uhr. Maßgeblich für den Countdown
sind die `ft8_armed`-Events des Servers.

### 4.6 Frontend

- **TX-Panel FT8:**
  - Nachrichten-Baukasten (Art, DX-Rufzeichen, Rapport, Freitext) mit
    Vorschau des tatsächlich gesendeten Texts.
  - Offset in Hz, Slot (nächster / 1. :00/:30 / 2. :15/:45),
    Wiederholungen, Driftkompensation.
  - Der PTT-Knopf wird zu „Scharf schalten“ mit Countdown bis zum Slot. Ein
    zweiter Klick bricht ab.
- **Slot-Uhr:** 15-s-Fortschrittsbalken, aktuelle Parität, UTC.
- **Empfang (Panel „Empfang“):** Tabelle UTC | dB | DT | Hz | Nachricht,
  nach Slots gruppiert, neueste oben, Filter „nur CQ“, eigenes Rufzeichen
  hervorgehoben.
- **Klick auf einen Decode:** übernimmt das Absender-Rufzeichen als
  DX-Rufzeichen und stellt die Gegenparität ein (geantwortet wird im anderen
  Slot). Die Nachrichtenart wählt der Betreiber weiter selbst.
- **Wasserfall:** Markierung des FT8-Audiobands (200–3000 Hz über der
  Dial-Frequenz) und des eigenen TX-Offsets (50 Hz breit).
- **Station:** Feld für Rufzeichen und Locator (siehe 4.2).
- ~~Kennzeichnung „experimentell“ am FT8-Modus, bis F4 abgeschlossen ist.~~
  Entfällt auf Wunsch des Betreibers (28.09.2026): keine solche Kennzeichnung
  in der Oberfläche.

### 4.7 SimBackend

- TX über dieselbe Serienlogik, in Tests mit vorgespulter Uhr.
- RX mit ein paar erfundenen Decodes pro Slot, darunter ein CQ und eine
  Antwort an das eigene Rufzeichen. So lassen sich Tabelle, Filter und
  Klick-zum-Antworten ohne Hardware entwickeln.
- `set_station` wie im echten Backend (nur im Speicher).

## 5. Schritte

Jeder Schritt endet mit grünen Tests (`ruff`, `pytest`, `npm run check`) und
einem Commit.

### F0 — Voraussetzungen auf dem Radio-Server

1. In der pluto-tx-Installation prüfen, ob `install-ft8.sh` gelaufen ist:
   `python3 -c "from pluto_tx.flowgraph import FT8_AVAILABLE; print(FT8_AVAILABLE)"`
   (im pluto-tx-Verzeichnis) und `which jt9`. Falls nicht: den Betreiber
   bitten, `install-ft8.sh` auszuführen. **Nicht selbst in pluto-tx
   installieren.**
2. Uhr prüfen: `timedatectl show -p NTPSynchronized --value` muss `yes`
   liefern.
3. Stationsdaten in `web-trx.env` eintragen: `WEB_TRX_STATION_CALL=DA2JH`,
   `WEB_TRX_STATION_LOCATOR=JO43`.

**Fertig, wenn:** FT8 ist in pluto-tx verfügbar, die Uhr ist synchron und
die Stationsdaten stehen in `web-trx.env`.

### F1 — Stationsdaten und Empfang

> **Erledigt am 28.09.2026** (F0 ebenso). Messwerte in `docs/PROJECT_PLAN.md`
> Abschnitt 9: Web-TRX dekodiert im eingeschwungenen Zustand gleich viel wie
> pluto-cli.

1. `web_trx/station.py` (Prüfung) mit Tests. Request `set_station`,
   Speichern in `settings.json`, `hello`/`station`-Event, Startwerte aus der
   Umgebung — in `SessionManager`, `SimBackend` und `GnuRadioBackend`.
2. Frontend: Stationsfeld; M17-Quellrufzeichen wird vorbelegt, wenn es leer
   ist.
3. `modes.py`: RX-Schema `ft8` plus Tabellen-Test gegen pluto-tx.
4. `GnuRadioBackend`-RX wie in 4.4, einschließlich `ft8_status` und
   Verfügbarkeit.
5. Direct Sampling für RTL-SDR (4.4).
6. NTP-Prüfung und `server_time` (4.5).
7. SimBackend-RX mit erfundenen Decodes (4.7).
8. Frontend: FT8 im RX-Modus, Decode-Tabelle, Filter, Slot-Uhr,
   NTP-Warnung, Wasserfall-Markierung. Im Browser gegen SimBackend prüfen.
9. Hardware:
   - RTL-SDR mit Direct Sampling (Q-Zweig beim RTL-SDR Blog V3) auf
     14,074 MHz USB,
   - mindestens 10 Slots mitlaufen lassen,
   - die Zahl der Decodes mit
     `pluto-cli rx ssb --device rtlsdr --direct-sampling q --freq 14074000 --digimode ft8 --json`
     (oder `pluto-advanced-rx`) am selben Signal vergleichen, **nacheinander,
     nicht gleichzeitig** — das Gerät kann nur ein Prozess belegen.
   - Ohne HF-Antenne: 2 m, 144,174 MHz.

**Fertig, wenn:**
- Web-TRX dekodiert echten Verkehr in ähnlicher Zahl wie pluto-tx.
- Die Stationsdaten überstehen einen Neustart.
- Alle Tests sind grün.

**→ Haltepunkt:** Bericht an den Betreiber.

### F2 — Sendeserie ohne Hardware

1. `web_trx/ft8_series.py` mit Tests (simulierte Uhr), siehe 6.
2. `modes.py`: TX-Schema `ft8`.
3. SimBackend-TX über die Serienlogik.
4. `SessionManager`: Zahl verbundener Clients an das Backend melden
   („Bediener weg“).
5. Frontend: TX-Panel FT8 mit Scharf/Countdown/Abbruch, Vorschau,
   Klick-zum-Antworten aus der Decode-Tabelle.
6. WebSocket-Tests und Browser-Test gegen SimBackend.

**Fertig, wenn:**
- Eine Serie lässt sich scharf schalten, sie zählt herunter, tastet im
  richtigen Slot auf und ab, wiederholt in derselben Parität und lässt sich
  in jedem Zustand abbrechen.
- Nach 30 s ohne Browser bricht sie ab.
- Jede Aussendung steht im TX-Log.

**→ Haltepunkt vor F3:** Freigabe für Test-Aussendungen einholen: Frequenz,
Leistung, Aufbau.

### F3 — Senden auf Kurzstrecke

1. `GnuRadioBackend`-TX wie in 4.3.
2. Test Pluto → RTL-SDR auf kurze Distanz, Pluto-Dämpfung −40 dB oder
   niedriger, Frequenz nach Vorgabe des Betreibers:
   - Dekodierung in Web-TRX (RX über RTL-SDR),
   - zusätzlich mit `jt9` bzw. WSJT-X,
   - DT ≈ +0,1 s wie in pluto-tx gemessen, Tonabstand 6,25 Hz,
   - die erste Aussendung nach einem Kaltstart gezielt prüfen (bekannte
     Drift, siehe 7).
3. Abbruch während „scharf“ und während der Aussendung, NOTAUS, Browser
   schließen („Bediener weg“) — jeweils mit echtem Gerät.

**Fertig, wenn:** 3 von 3 Aussendungen werden dekodiert, alle
Abbruchwege funktionieren, und das TX-Log ist vollständig.

**→ Haltepunkt vor F4:** erst, wenn pluto-tx' FT8 selbst im Funkbetrieb
bestätigt ist und der Betreiber freigibt.

### F4 — Funkbetrieb

Ein QSO mit einer echten Station nach Vorgabe des Betreibers.

### F5 — Komfort (optional)

Band-Voreinstellungen (Dial-Frequenzen), Hinweis, wenn das eigene
Rufzeichen angerufen wird.

## 6. Tests

- **Ohne Hardware (auch in CI):**
  - `station.py`: gültige und ungültige Rufzeichen und Locator.
  - `modes.py`: Struktur, Grenzen, Tabellen gegen pluto-tx.
  - `ft8_series.py` mit simulierter Uhr:
    - Parität,
    - später Einstieg in einen laufenden Slot,
    - Wiederholung alle 30 s,
    - Obergrenze,
    - Abbruch durch PTT, NOTAUS, Trennen, Moduswechsel und „Bediener weg“,
    - **kein Auftasten nach einem Abbruch**.
  - Planer des SimBackend gegen `pluto_tx.ft8.plan_transmission`, übersprungen
    ohne pluto-tx.
  - WebSocket-Ablauf scharf → keyed → unkeyed → TX-Log.
  - Browser-Test der Panels gegen SimBackend.
- **Radio-Server:** die Hardware-Schritte aus F1 und F3, bei Bedarf als
  weitere mit `WEB_TRX_HW_TESTS=1` geschützte Tests in
  `test_radio_backend.py`, nur empfangend.

## 7. Risiken

- **Server-Uhr:** Ohne NTP gibt es keine Dekodierung und keine Antworten.
  Abhilfe: Prüfung und sichtbare Warnung (4.5).
- **Oszillatordrift des Pluto:** In pluto-tx per Vorverzerrung gemessen und
  kompensiert, aber nur an einem Gerät vermessen. Bekannt: Die erste
  Aussendung nach einem Kaltstart driftet am stärksten.
- **Ein Pluto für RX und TX:** pluto-tx dokumentiert, dass eine Änderung der
  RX-Bandbreite während einer Aussendung den Sendetakt verschiebt. Die
  RX-Bandbreite darf sich deshalb nicht ändern lassen, solange eine Serie
  scharf ist.
- **pluto-tx-Reife:** FT8 ist dort noch nicht im Funkbetrieb bestätigt.
  Web-TRX kann nicht besser sein als die Grundlage, daher die Reihenfolge
  der Phasen.

## 8. Entscheidungen

| # | Frage | Stand |
|---|---|---|
| 1 | Sendeserien ohne anwesenden Bediener | **Bestätigt (28.09.2026):** Abbruch nach 30 s ohne verbundenen Browser, höchstens 20 Wiederholungen. |
| 2 | Stationsdaten | **Entschieden:** eigene Betreiber-Einstellung (4.2); diese Station: DA2JH, JO43 |
| 3 | Automatischer QSO-Ablauf | Nicht vorgesehen (wie pluto-tx) |
| 4 | Zeitpunkt | **Bestätigt (28.09.2026):** F1/F2 sofort; F3 nach Freigabe; F4 erst nach pluto-tx' eigenem Funktest |
