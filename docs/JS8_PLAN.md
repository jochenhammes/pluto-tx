# JS8 (JS8Call) in pluto-tx — Plan für RX, TX, CLI und Web

## Context

JS8 ist JS8Calls Tastatur-Chat-Modus auf FT8-Technik:
- dieselbe 8-GFSK-Modulation mit 79 Symbolen, Costas-Sync und LDPC,
- aber freie Texte über **mehrere Rahmen**,
- gerichtete Nachrichten (`@CALL`, `@ALLCALL`) und Kommandos (`SNR?`, `HEARTBEAT` …),
- vier Geschwindigkeiten (Normal / Fast / Turbo / Slow).

pluto-tx kann FT8 in allen vier Programmen: TX-App, RX-App, `pluto-cli` und
Web-TRX (FT8-Senden seit PR #2, erster Funktest 29.09.2026). JS8 soll
dieselbe Breite bekommen.

Ausgeführt wird dieser Plan **lokal auf dem Radio-Server** von einer
Claude-Code-Session; dort stehen die SDRs zum Testen bereit:
- **Senden bevorzugt mit dem Pluto.** Er hat gerade keine RX-Antenne.
- **Empfang mit RTL-SDR oder HackRF.** Auf KW nutzt der RTL-SDR Direct
  Sampling (Q-Zweig).

Start in der lokalen Session:

> Lies `docs/JS8_PLAN.md` und führe ihn Phase für Phase aus. Halte an jedem
> HALT an.

**Wichtige Randbedingung:** Der Pluto sendet nur ab ca. 70 MHz, KW geht
mit ihm nicht. Funktests mit dem Pluto laufen deshalb auf **2 m oder
70 cm**; empfangen wird mit dem RTL-SDR. KW-Empfang echter JS8-Stationen
(7,078 / 14,078 MHz) läuft über den RTL-SDR. KW-Senden geht nur mit dem
HackRF (optional, J7-H5).

---

## 0. Regeln für die ausführende Session

- **Arbeitsort:** pluto-tx `main`, das ganze Repo ist frei bearbeitbar.
- **Kleine Commits** je abgeschlossenem Schritt; Push am Ende jeder Phase.
  Größere Phasen (J3, J6) als PR wie bei FT8 (PR #1/#2), damit die CI vor
  dem Merge läuft.
- **FT8 darf sich nicht ändern.** Wiederverwendung nur additiv. Ein
  Prüfsummentest stellt sicher, dass `ft8.encode_iq()` byte-identisch
  bleibt.
- **Vor jedem Commit laufen diese Tests grün:**
  - `QT_QPA_PLATFORM=offscreen PLUTO_WEBTRX_CONTROL=off python3 -m unittest discover tests`
    (Repo-Wurzel)
  - `cd web-trx/backend && .venv/bin/python -m pytest && .venv/bin/ruff check web_trx tests`
  - `cd web-trx/frontend && npm run check`
- **Senden (Freigabe des Betreibers DA2JH vom 29.09.2026):** Die Session
  darf **selbstständig** senden, ohne vorher zu fragen, solange alle diese
  Grenzen eingehalten sind:
  - **nur innerhalb der Amateurfunkbänder.** Vor jeder Aussendung wird die
    Frequenz (Träger plus Audio-Offset plus Signalbandbreite) gegen die
    Bandgrenzen geprüft (`pluto_tx.config.in_amateur_band()`; J3 ergänzt
    dort additiv 40 m und 20 m). Bevorzugt werden die Digital- bzw.
    Schmalband-Segmente des Bandplans, auf KW die JS8-Frequenzen.
  - **nur mit kleiner Sendeleistung:**
    - Pluto mit mindestens 40 dB TX-Dämpfung,
    - HackRF ohne RF-Verstärker und mit niedriger VGA-Stufe,
    - erhöht wird nur, wenn der Empfang sonst nicht reicht, schrittweise
      und nie über 1 mW (0 dBm) am Ausgang.
  - Jede Aussendung trägt das Rufzeichen **DA2JH**; bei JS8 ist es ohnehin
    im Rahmenkopf.
  - **Keine Dauer- oder Endlosaussendungen:**
    - Serien sind begrenzt, höchstens 20 Aussendungen am Stück wie bei FT8,
    - die Session bleibt während jeder Aussendung aktiv und überwacht sie,
    - NOTAUS, Abbruch und Watchdog müssen funktionieren (J3/J7-H3 zuerst
      ohne Antenne bzw. an der Dummy-Load prüfen).
  - **Jede Aussendung wird protokolliert** in `docs/js8/TESTS.md`: UTC-Zeit,
    Gerät, Frequenz, Leistung bzw. Dämpfung, Inhalt, Ergebnis.
  - Automatische Antworten oder Heartbeats als **Funktion** (J9) sind davon
    getrennt und brauchen eine eigene Entscheidung des Betreibers.
- **Nichts erfinden.** Jede Protokollkonstante wird aus dem gepinnten
  JS8Call-Quelltext belegt: Datei, Zeile, Commit stehen in
  `docs/js8/SPEC.md`. Was dort nicht eindeutig ist → **HALT**, berichten.
- **HALT-Punkte:**
  - Ende J0 (Machbarkeit, Entscheidung Decoder),
  - bei jeder Abweichung der Referenzvektoren,
  - wenn eine Sendegrenze aus diesem Abschnitt nicht einzuhalten wäre.

---

## 1. Was wiederverwendet wird (Bestand auf `main`)

| Baustein | Datei | Nutzung für JS8 |
|---|---|---|
| GFSK-Synthese (Port von ft8_lib) | `pluto_tx/ft8.py` `_gfsk_phase()` | Wird additiv als `ft8.gfsk_iq(symbols, tone_hz, symbol_period, bt, rate)` veröffentlicht. JS8 übergibt Symboldauer und Tonabstand je Speed. |
| Slotplanung | `ft8.plan_transmission(..., slot_s=)`, `next_slot_start`, `current_slot_start` | Schon für beliebige `slot_s` gebaut (15/10/6/30 s). |
| Zeitgenaue Quelle | `pluto_tx/ft8_source.py` `Ft8TimedSource` | Unverändert; hält die Stille bis `start_at`. |
| Drift-Vorkompensation Pluto | `PlutoTxFlowgraph.ft8_drift_hz_per_s()`, `config.FT8_DRIFT_MODEL` | Additiv für jede Rahmendauer; bei Slow (30 s, 3,125 Hz Tonabstand) besonders wichtig. |
| TX-Zweig im Flowgraph | `flowgraph.py` FT8-Zweig (`ft8_source` → `ft8_tx_resampler`, `ft8_to_audio`), `_ft8_start_source()`, `ft8_hold_s` | Eigener JS8-Zweig nach gleichem Muster (MODE_JS8), inkl. Soundcard/AIOC-Audio. |
| Bandprüfung | `pluto_tx/config.py` `DE_AMATEUR_BANDS_HZ`, `in_amateur_band()` | Sendegrenze aus Abschnitt 0; 40 m und 20 m additiv ergänzen. |
| RX-Slotzerteilung | `pluto_advanced_rx/ft8_rx.py` `Ft8Receiver` (Ringpuffer, Wanduhr-Slots, Decode-Thread) | Muster für `Js8Receiver` mit Slotlänge je Speed. |
| Decoder-Subprozess + WAV | `pluto_advanced_rx/ft8_decoder.py` (jt9-Aufruf, `parse_jt9_output`) | Muster für den Referenz-Decoder `js8`, falls der offline dekodiert (J0). |
| CLI-Serie | `pluto_cli/runtime.py` `_run_ft8_series`, `tx.py add_ft8_subparser`, `rx.py --digimode ft8` | Gleiche Struktur für `tx js8` / `rx --digimode js8`. |
| Web-Serie | `web-trx/backend/web_trx/ft8_series.py` (reines Python, injizierte Uhr, harte Cancel-Regel) | Muster für `js8_series.py`: aufeinanderfolgende Slots statt Parität. |
| Web-Schema / Features / Sim | `modes.py` (`_ft8_tx`, `client_options`), `radio_backend.py` (`_arm_ft8`, `_on_ft8_decodes`), `sim_backend.py` | Gleiches Muster für `js8`. |
| Uhr/NTP | `web-trx/backend/web_trx/clock.py` | Wird mitbenutzt. |
| Test-Fakes | `tests/fakes.py` (`register_tx`, Fake-RX-Gerät), `tests/test_ft8.py` (Codec-/Slot-/Flowgraph-/GUI-Tests) | Vorlage für `tests/test_js8_*.py`. |
| Installer-Muster | `install-ft8.sh` (gepinnter Clone ins ignorierte Verzeichnis, Build, ctypes-Prüfung, apt `wsjtx`) | Vorlage für `install-js8.sh`. |

---

## 2. Protokoll — was feststeht und was J0 belegen muss

Grundlage ist ein **gepinnter JS8Call-Quelltext** (GPLv3, kompatibel mit
pluto-tx GPLv3). Kandidaten sind das Original `js8call/js8call` (Tag der
letzten Release) und der aktiv gepflegte Fork „JS8Call-improved“. J0 wählt
nach Verfügbarkeit, Build-Aufwand und Interop mit dem, was auf 40 m und
20 m tatsächlich läuft.

Die folgenden Punkte sind **Arbeitsannahmen**. Jede wird in J0 mit
Datei/Zeile belegt oder korrigiert, bevor J1 beginnt:

| Thema | Arbeitsannahme | Belegen in |
|---|---|---|
| Modulation | 8-GFSK wie FT8, 79 Symbole (3 × 7 Costas + 58 Daten), Gauß-Pulsformung | JS8Call Fortran/C++ Modulator (`genjs8*`, Tx-Pfad) |
| Kanalcode | LDPC(174,87) wie **FT8 v1** (WSJT-X 1.9), 75 Nutzbits + 12-bit CRC — **nicht** ft8_libs (174,91)/CRC14 | `ldpc_174_87_params.f90` o. ä., CRC-Routine |
| Costas | andere Arrays als FT8; bei neueren Speeds je Position (Anfang/Mitte/Ende) verschieden | `js8*_params.f90` / Modulator |
| Speeds (Submodes) | Normal 15 s / 0,16 s / 6,25 Hz · Fast 10 s / 0,10 s / 10 Hz · Turbo 6 s / 0,05 s / 20 Hz · Slow 30 s / 0,32 s / 3,125 Hz (Ultra deaktiviert) | Submode-Tabelle (NSPS, Periode, Startverzögerung) |
| Sendebeginn im Slot | Speed-abhängige Verzögerung (Normal ~0,5 s) | Submode-Tabelle |
| Rahmen | 72 Bit Nutzlast + 3 Bit Rahmenflags (erster/letzter Rahmen einer Übertragung, Datenrahmen) | `varicode.cpp` (FrameType, Transmission-Flags) |
| Nachrichtenschicht | Rahmentypen Heartbeat, Compound, CompoundDirected, Directed, Data, DataCompressed; Rufzeichen-/Grid-Packing; Kommando-Tabelle; Freitext per Huffman bzw. JSC-Wörterbuch | `varicode.cpp`, `jsc*.cpp`, Kommandoliste |
| Übertragung | Mehrere Rahmen einer Nachricht in **aufeinanderfolgenden** Slots, Kopf mit „ABSENDER:“ | Tx-Queue in JS8Call (`MainWindow`/`Varicode::buildMessageFrames`) |
| Anrufffrequenzen | USB-Dial 7,078 / 14,078 MHz (u. a.); Heartbeat-Unterband im Audio 500–1000 Hz | Frequenzliste in JS8Call |

**Ergebnis von J0:**
- `docs/js8/SPEC.md` mit Tabellen, Konstanten, Quellenangaben und
  Commit-Hash.
- Referenzvektoren in `tests/data/js8/`: Text → Rahmen → Bits → Töne, und
  WAVs, die JS8Call selbst erzeugt hat. Die WAVs sind 12 kHz/16 bit, kurz,
  insgesamt ≤ 3 MB.

---

## 3. Architektur und Dateien

### pluto-tx (Kern, TX, RX, CLI)

```
install-js8.sh                     NEU: gepinnter JS8Call-Clone nach js8call/ (gitignored),
                                   Tabellen-Extraktion, optional Build des Referenz-Decoders `js8`
pluto_tx/js8_phy.py                NEU: Submode-Tabelle, CRC12, LDPC(174,87)-Encoder (+ Paritätsmatrix
                                   für RX), Costas, Rahmenbits -> 79 Töne, Töne -> Bits (für RX)
pluto_tx/js8_message.py            NEU: Nachrichtenschicht: Rufzeichen/Grid-Packing, Kommandos,
                                   Huffman/JSC, Text -> Rahmenliste (mit First/Last-Flags),
                                   Rahmen -> Text (Unpack), compose(kind, ...) wie ft8.compose
pluto_tx/js8.py                    NEU: encode_frames_iq(frames, submode, tone_hz, rate) über
                                   ft8.gfsk_iq(); plan_frames(now, submode, n) -> Slotzeiten
                                   (aufeinanderfolgend); can_encode(); Konstanten
pluto_tx/js8_tables.py             NEU: lädt die in J0/J1 erzeugten Tabellen (LDPC, Costas,
                                   Kommandos, Huffman) -- klein, eingecheckt mit GPL-Hinweis;
                                   das große JSC-Wörterbuch (falls groß) aus js8call/ zur Laufzeit
pluto_tx/ft8.py                    ADDITIV: gfsk_iq() als öffentliche Hülle um _gfsk_phase()
pluto_tx/flowgraph.py              ADDITIV: MODE_JS8, JS8-Zweig (Muster FT8), set_js8_frames(),
                                   prepare_js8(), js8_start_at, js8_hold_s, js8_problem();
                                   Drift-Kompensation über die Rahmendauer
pluto_tx/config.py                 ADDITIV: JS8_* (Default-Offset, Bereich, Pegel, Speeds, Key-early,
                                   Latenz = FT8-Werte als Start); DE_AMATEUR_BANDS_HZ + 40 m, 20 m
pluto_tx/gui.py                    ADDITIV: Digimode "JS8": Composer (An: CALL/@ALLCALL, Text,
                                   Speed, Offset, Vorschau "N Rahmen, ~T s"), Senden/Abbrechen
pluto_advanced_rx/js8_decoder.py   NEU: Slot-Decoder mit Backends "js8" (Referenz-Binary, falls
                                   offline nutzbar) und "own" (numpy-Port des ft8_lib-Verfahrens
                                   mit JS8-Parametern); Js8Frame(text?, bits, snr, dt, freq, flags)
pluto_advanced_rx/js8_rx.py        NEU: Js8Receiver (Muster Ft8Receiver; Slot je Speed, optional
                                   mehrere Speeds parallel), Js8State (Qt-frei, Lock+Snapshot)
pluto_advanced_rx/js8_assembly.py  NEU: Zusammensetzen mehrteiliger Nachrichten je (Frequenz ±
                                   Toleranz, Speed): First..Last, Timeout, Absender aus Kopf;
                                   Stationsliste (Call, Grid, SNR, zuletzt gehört, Offset)
pluto_advanced_rx/flowgraph.py     ADDITIV: active_digimode == "js8" (Muster FT8: USB -> 12 kHz ->
                                   Bandfilter -> Js8Receiver)
pluto_advanced_rx/config.py        ADDITIV: JS8_*
pluto_advanced_rx/gui.py           ADDITIV: Digimode "JS8": Band-Aktivität (je Offset letzter Text),
                                   Stationen, Nachrichtenverlauf; Speed-Auswahl
pluto_cli/tx.py, rx.py, runtime.py ADDITIV: `tx js8`, `rx --digimode js8`, Serienlauf
tests/test_js8_phy.py, test_js8_message.py, test_js8_codec_roundtrip.py,
tests/test_js8_flowgraph.py, test_js8_rx.py, test_js8_cli.py, test_js8_gui.py   NEU
tests/data/js8/                    NEU: Referenzvektoren + kurze Referenz-WAVs (JS8Call-erzeugt)
tools/js8_extract_tables.py, tools/js8_record_slots.py   NEU (J0)
docs/js8/SPEC.md, docs/js8/TESTS.md                       NEU
.gitignore                         + js8call/
```

### Web-TRX

```
web-trx/backend/web_trx/modes.py        ADDITIV: js8 RX-Schema (decoder, submodes) und TX-Schema (kind, to,
                                        text, submode, offset_hz); Tabellen per Test gegen pluto-tx geprüft
web-trx/backend/web_trx/js8_series.py   NEU: Rahmenfolge in aufeinanderfolgenden Slots, cancel-sicher
                                        (Regel wie ft8_series: nach Cancel nie wieder key())
web-trx/backend/web_trx/radio_backend.py ADDITIV: RX js8 (Js8Receiver -> Events js8_frame/js8_message/
                                        js8_status), TX js8 (_arm_js8), Features, Watchdog/NOTAUS/Disconnect
                                        brechen die Serie ab
web-trx/backend/web_trx/sim_backend.py  ADDITIV: Loopback eigener JS8-Rahmen in die Empfangsliste
web-trx/backend/web_trx/session.py      ADDITIV: TX-Log mit gesendetem Text; No-Operator bricht js8 ab
web-trx/frontend/src/lib/Js8Panel.svelte NEU: Chat-Ansicht (Konversationen je Station, @ALLCALL),
                                        Band-Aktivität, Stationsliste, Composer mit Vorschau/Rahmenzahl,
                                        Countdown, Abbrechen; Klick auf Station setzt "An:"
web-trx/frontend/src/App.svelte, lib/Waterfall.svelte  ADDITIV: Modus JS8, TX-/RX-Marker je Speed-Bandbreite
web-trx/backend/tests/test_js8_*.py, test_modes.py, test_server_ws.py, test_sim_backend.py  NEU/ADDITIV
web-trx/docs/JS8.md                     Betrieb, Grenzen
```

### Entscheidungen, die der Plan vorschlägt

Abweichungen davon nur nach Rückfrage.

1. **Umfang v1:** Empfang und Dekodierung **aller** Rahmen- und
   Kommandotypen. Senden kann v1:
   - CQ und Heartbeat (manuell),
   - `@ALLCALL`-Text und gerichteten Text an ein Rufzeichen,
   - `SNR?`, die SNR-Antwort, ACK und ungerichteten Freitext.

   **Keine automatischen Antworten**, kein Relay, keine Inbox und kein
   Store-and-Forward (optional J9, nur mit eigener Entscheidung).
2. **Speeds:** RX Normal, Fast, Turbo und Slow (einzeln oder „alle“),
   TX alle vier. Standard ist Normal.
3. **Decoder:** Die J0-Messung entscheidet.
   - Primär: das Referenz-Binary `js8`, falls es WAVs offline dekodiert
     (wie `jt9 -8` bei FT8) und in angemessener Zeit.
   - Immer vorhanden: der **eigene** numpy-Decoder als Fallback und als
     unabhängige Gegenprobe; ohne ihn kein RX ohne JS8Call-Build.
4. **Tabellen:**
   - Kleine Tabellen (LDPC, Costas, Kommandos, Huffman) werden aus dem
     gepinnten Quelltext extrahiert und **eingecheckt**, mit GPL-Hinweis
     und Quellenangabe.
   - Das JSC-Wörterbuch wird nur bei Größe über 1 MB zur Laufzeit aus
     `js8call/` geladen.
   - Fehlt JSC, erscheinen komprimierte Rahmen als „[JSC n Bytes]“; das
     Senden nutzt dann Huffman.

---

## 4. Phasen, Schritte und Tests

### J0 — Referenz und Machbarkeit (auf dem Radio-Server, ohne Senden)

1. JS8Call-Quelltext in ein Wegwerf-Verzeichnis klonen
   (`~/js8-ref/`, später Teil von `install-js8.sh`). Commit bzw. Tag
   festhalten.
2. Belegen und in `docs/js8/SPEC.md` festhalten:
   - alle Punkte der Tabelle in Abschnitt 2,
   - die Bitreihenfolge von Rahmen → LDPC → Graycode → Töne,
   - die CRC-Polynomial und Init,
   - die Costas-Positionen je Speed,
   - Slot- und Startverzögerung je Speed.
3. `apt-cache policy js8call`, `dpkg -L js8call` prüfen, oder JS8Call aus
   dem Quelltext bauen (cmake; gfortran, fftw, Qt5). Festhalten:
   - ob ein Decoder-Binary `js8` existiert,
   - ob es **WAV-Dateien offline** dekodiert (Aufruf und Ausgabeformat),
   - Laufzeit je 15-s-Slot.
4. Referenzmaterial erzeugen:
   - **TX-Referenz:** JS8Call auf ein virtuelles Audiogerät senden lassen
     (PipeWire/ALSA-Loopback; siehe `remove-pipewire-loopback-nodes.sh`)
     und als WAV aufnehmen. Je Speed:
     - CQ,
     - Heartbeat,
     - `@ALLCALL` mit 60 Zeichen,
     - gerichtete Nachricht,
     - `SNR?`.
   - Dazu die von JS8Call gezeigten bzw. geloggten Rahmentexte notieren.
   - **Off-Air:** 30 min RTL-SDR (Direct Sampling Q) auf 7,078 und
     14,078 MHz USB als 12-kHz-WAV je Slot mitschneiden
     (Hilfsskript `tools/js8_record_slots.py`). Parallel dazu die
     Referenz-Decodes, per `js8`-Binary oder JS8Call am Loopback.
5. Tabellen-Extraktor `tools/js8_extract_tables.py` schreiben. Er erzeugt
   `pluto_tx/js8_tables.py` deterministisch aus dem gepinnten Quelltext;
   ein zweiter Lauf ergibt ein identisches Ergebnis.
6. **HALT:** Ergebnis berichten:
   - SPEC-Zusammenfassung,
   - Decoder-Entscheidung,
   - JSC-Größe,
   - Menge der Referenzdaten,
   - Abweichungen von den Arbeitsannahmen.

**Tests J0:**
- `tests/test_js8_tables.py`: Die Tabellen haben die erwarteten
  Dimensionen (H: 87 × 174, Costas-Längen 7, 79 Töne).
- Der Extraktor ist reproduzierbar (Hash).

### J1 — Kern-Codec (reines Python, ohne Hardware)

1. `ft8.gfsk_iq()` additiv veröffentlichen.
   **Test:** Prüfsumme von `ft8.encode_iq()` für drei Nachrichten ist
   unverändert (vorher festhalten).
2. `js8_phy.py`: CRC12, LDPC-Encode, Graycode, Costas-Einbettung,
   Töne ↔ Bits.
3. `js8_message.py`:
   - Packing und Unpacking aller Rahmentypen,
   - `build_frames(from_call, to, text, kind) -> [Frame]` mit
     First/Last-Flags,
   - `compose()`.
4. `js8.py`:
   - `encode_frames_iq(frames, submode, tone_hz, rate, tail_s)` → `(iq, dauer)`,
   - `frame_count()`, `plan_frames()`.

**Tests J1:**
- **`test_js8_phy.py`:**
  - CRC-Vektoren aus der SPEC.
  - Jedes erzeugte Codewort erfüllt H·c = 0.
  - Bit ↔ Ton rundlaufend.
  - Costas steht an den richtigen Positionen.
  - Die Dauer je Speed ist 79 × Symboldauer.
- **`test_js8_message.py`:**
  - Die Referenzvektoren aus J0 stimmen **bitgenau**: Text → Rahmen →
    75 Bit → 79 Töne, für jeden Rahmentyp und jeden Speed.
  - Rundlauf Text → Rahmen → Text für Zeichensatzgrenzen, Rufzeichen mit
    `/P`, Compound und Grid.
  - Zu lange oder unzulässige Eingaben werden mit einer klaren Meldung
    abgelehnt.
- **`test_js8_codec_roundtrip.py`:**
  - Waveform → Töne zurückgewinnen (ideal, ohne Rauschen).
  - Spektrum: Die belegte Bandbreite ist ≈ 8 × Tonabstand (Normal 50 Hz,
    Fast 80, Turbo 160, Slow 25).

### J2 — Empfang (ohne Hardware)

1. `js8_decoder.py`:
   - **Backend `own`:** Port des ft8_lib-Verfahrens mit JS8-Parametern:
     Wasserfall je Speed (Zeitauflösung 2 und Frequenzauflösung 2 wie
     `monitor_process`), Kandidatensuche über die Costas-Arrays,
     LLR-Berechnung, BP-Decoder (LDPC 174,87), CRC-Prüfung,
     Duplikatunterdrückung, SNR-Schätzung.
   - **Backend `js8`** (falls J0 positiv): Subprozess auf eine WAV-Datei,
     Parser nach dem Muster von `parse_jt9_output`.
2. `js8_rx.py`: `Js8Receiver` (Slotlänge je Speed; „alle Speeds“ =
   parallele Ringpuffer-Auswertung zu den jeweiligen Slotgrenzen) und
   `Js8State`.
3. `js8_assembly.py`:
   - Zusammensetzen mehrteiliger Nachrichten nach Offset ±
     halbe Bandbreite und Speed.
   - Timeout nach 2 fehlenden Slots: Die Nachricht wird mit „…“ als
     unvollständig markiert.
   - Die Stationsliste pflegen.
4. RX-Flowgraph `active_digimode == "js8"`.

**Tests J2 (`test_js8_rx.py`):**
- **Dekodierrate:** Eigene Waveform plus AWGN, SNR-Sweep −10 … −26 dB,
  Offsets 200–2800 Hz, DT −1 … +2 s, Drift ±0,5 Hz über den Rahmen.
  Schwellen:
  - Normal ≥ 95 % bei −18 dB,
  - Slow ≥ 95 % bei −22 dB,
  - die genauen Werte nach der J0-Messung mit dem Referenz-Decoder
    festlegen und im Test dokumentieren.
- **Referenz-WAVs aus J0** (von JS8Call erzeugt): alle Rahmen dekodiert,
  Texte exakt.
- **Off-Air-Mitschnitte aus J0:** Der eigene Decoder findet
  ≥ 80 % der Referenz-Decodes. Jeder eigene Decode, der dort fehlt, wird
  einzeln angesehen: Er darf nicht falsch sein, CRC-Fehlerrate 0 bei
  1000 Rauschslots.
- **Zusammensetzen:**
  - drei Rahmen in Folge → eine Nachricht,
  - ein fehlender Mittelrahmen → als unvollständig markiert,
  - zwei Stationen gleichzeitig auf verschiedenen Offsets → getrennt.
- **Flowgraph mit Fake-RX-Gerät:** Die Slotgrenzen folgen der injizierten
  Uhr (Muster `RxFlowgraphTests` in `test_ft8.py`).

### J3 — Senden im Flowgraph (ohne Hardware)

1. MODE_JS8 in `PlutoTxFlowgraph`:
   - eigener Zweig nach FT8-Muster,
   - `set_js8_message(frames, submode, tone_hz)`,
   - `prepare_js8()` synthetisiert **alle Rahmen einer Nachricht** vorab,
   - `js8_start_at`,
   - `js8_hold_s` für einen einzelnen Rahmen.

   Mehrere Rahmen gehen als einzelne Tastungen in aufeinanderfolgenden
   Slots raus, ein Rahmen je Slot, jeder zeitgenau über `Ft8TimedSource`
   (Klasse unverändert wiederverwendet).
2. Drift-Kompensation je Rahmen über `ft8_drift_hz_per_s()`; der Ansatz
   ist unabhängig vom Modus.
3. Soundcard/AIOC: realer Anteil als Audio, wie bei FT8.
4. Sicherheit wie bei FT8:
   - Auto-Unkey-Watchdog,
   - NOTAUS bricht die Rahmenfolge ab,
   - kein Rahmen nach einem Abbruch.
5. `config.DE_AMATEUR_BANDS_HZ` additiv um 40 m (7,000–7,200 MHz) und
   20 m (14,000–14,350 MHz) ergänzen. `js8_problem()` lehnt Frequenzen
   außerhalb der Bänder ab: Die Prüfung gilt für die gesamte belegte
   Bandbreite aus Träger, Offset und Signalbreite.

**Tests J3 (`test_js8_flowgraph.py`, Fake-TX-Gerät):**
- Das Signal beginnt bei `js8_start_at`, mit Toleranz wie in den
  FT8-Tests.
- Die Dauer je Speed stimmt.
- Drei Rahmen landen in drei aufeinanderfolgenden Slots.
- Abbruch nach Rahmen 1: Rahmen 2 wird nicht gesendet.
- NOTAUS während eines Rahmens: sofort still, Safe-State-Aufruf erfolgt.
- Der Mitschnitt der Fake-Senke wird durch den eigenen Decoder aus J2
  exakt dekodiert.
- `js8_problem()` meldet einen Fehler für eine Frequenz außerhalb der
  Bänder und für eine Frequenz an der Bandkante, bei der das Signal über
  die Kante reicht.
- Die FT8-Tests laufen unverändert grün.

### J4 — CLI

- `pluto-cli tx js8` mit den Optionen:
  - `--kind {cq,hb,allcall,directed,snr_query,snr_reply,ack,free}`,
  - `--to CALL`, `--text`, `--submode {normal,fast,turbo,slow}`,
  - `--offset-hz`, `--mycall`, `--mygrid`.

  Ausgabe: `--json`-Events `js8_waiting`, `keyed`, `unkeyed`,
  `js8_frame_sent`, `js8_done`, `js8_cancelled`. Strg-C bricht sicher ab.
- `pluto-cli rx … --digimode js8 --js8-submode {normal,fast,turbo,slow,all}
  --js8-decoder {auto,js8,own}`. Events: `js8_frame`, `js8_message`,
  `js8_slot`.
- `pluto_cli/README.md` ergänzen.

**Tests J4 (`test_js8_cli.py`):**
- Argumentprüfung.
- `tx js8` mit Fake-Gerät: Event-Folge und Anzahl der Rahmen.
- Strg-C (SIGINT) mitten in der Folge führt zum Safe-State-Aufruf.
- `rx` mit Fake-RX, das eine JS8-WAV einspeist, liefert `js8_message`.

### J5 — Qt-Apps

- **TX-App**, Digimode „JS8“:
  - Felder An (CALL oder @ALLCALL), Art, Text, Speed und Offset.
  - Vorschau: gepackter Kopf, „N Rahmen ≈ T s“.
  - Senden, Abbrechen und ein Countdown bis zum Slot.
  - Die Stationsdaten kommen aus denselben Feldern wie bei FT8/M17.
- **RX-App**, Digimode „JS8“:
  - Tabelle Band-Aktivität (Offset, letzter Text, SNR, Zeit),
  - Stationsliste,
  - Nachrichtenfenster; ein Doppelklick übernimmt Call und Offset in die
    Zwischenablage bzw. in das Feld „An“ der TX-App, wenn beide laufen.
- Der Wasserfall zeigt die JS8-Bandbreite je Speed am Cursor.

**Tests J5 (`test_js8_gui.py`, offscreen):**
- Die Einträge im Digimode-Menü sind vorhanden.
- Die Vorschau zeigt die richtige Rahmenzahl.
- Senden ruft `prepare_js8` und die Serie auf (gemockt).
- `Js8State` erscheint in der Tabelle.
- Der bestehende Layout-Test bleibt grün.

### J6 — Web-TRX

1. `modes.py`: RX-Schema js8 (`submode`, `decoder`) und TX-Schema js8
   (`kind`, `to`, `text`, `submode`, `offset_hz`), `client_options`.
   Die Tabellen werden per Test gegen pluto-tx geprüft.
2. `js8_series.py`:
   - Rahmen in aufeinanderfolgenden Slots,
   - `plan` bzw. `key`/`unkey` injiziert,
   - Events `js8_armed`, `js8_frame`, `js8_done`, `js8_cancelled`,
   - harte Cancel-Regel: nach einem Abbruch wird nie wieder getastet,
     auch nicht, wenn der Abbruch während `key()` kommt.
3. `radio_backend.py`:
   - RX: `Js8Receiver` → Events `js8_frame`, `js8_message`, `js8_status`.
   - TX: `_arm_js8`.
   - NOTAUS, Disconnect, der No-Operator-Watchdog, ein Moduswechsel und
     das Herunterfahren brechen die Serie ab.
   - Features: `js8.tx`, `js8.rx_backends`, `js8.submodes`.
4. `sim_backend.py`: Loopback der eigenen Rahmen in RX; ein simulierter
   Nachbar-Sender für die Oberfläche.
5. Frontend `Js8Panel.svelte`:
   - Chat nach Konversation,
   - @ALLCALL,
   - Band-Aktivität,
   - Stationen (SNR, Grid, zuletzt),
   - Composer mit Rahmenzahl und Dauer,
   - Countdown und Abbrechen.

   Im Wasserfall kommen TX- und RX-Marker für die JS8-Bandbreite hinzu.
6. Die Doku `web-trx/docs/JS8.md` und die README ergänzen.

**Tests J6:**
- **`test_js8_series.py`** mit simulierter Uhr:
  - Folge und Zeiten,
  - Abbruch in jedem Zustand,
  - Abbruch während `key()`,
  - höchstens eine Nachricht gleichzeitig.
- **`test_modes.py`:** Schema und Ablehnungen.
- **`test_server_ws.py`:** Sim-Loopback ergibt `js8_message` im Browser-Protokoll.
- **`test_sim_backend.py`.**
- **`npm run check`.**
- **Playwright-Smoke gegen das sim-Backend:**
  - Nachricht an @ALLCALL senden → sie erscheint in der Konversation,
  - Abbrechen stoppt den Countdown.
- **Bestehende FT8-Tests** bleiben grün.

### J7 — Hardwaretests (auf dem Radio-Server)

Die Session sendet hier selbstständig, innerhalb der Grenzen aus
Abschnitt 0.

Aufbau:
- Der **Pluto sendet** mit mindestens 40 dB TX-Dämpfung auf 2 m oder
  70 cm, im Schmalband- bzw. Digitalsegment des Bandplans.
- Ein **RTL-SDR empfängt**, alternativ der HackRF.

Die Abbruchwege (H3) werden **zuerst** geprüft, noch vor den längeren
Serien. Alle Empfangs-Slots werden zusätzlich als WAV mitgeschnitten und
mit dem Referenz-Decoder gegengeprüft. Jede Aussendung kommt ins
Protokoll `docs/js8/TESTS.md`.

| Test | Inhalt | Kriterium |
|---|---|---|
| H1 KW-Empfang | RTL-SDR Direct Sampling, 7,078 und 14,078 MHz, je 30 min; nacheinander über RX-App, CLI und Web | Eigene Decodes ≥ 80 % der Referenz auf denselben Slots, keine falschen Texte, Stationsliste plausibel |
| H3 Abbruchwege (zuerst) | Scharf (vor dem Slot), während Rahmen 1 von 3, NOTAUS, Browser schließen (Web), Strg-C (CLI), App schließen | Sofort still, kein weiterer Rahmen, Pluto im Safe-State (`iio_attr`: TX-Dämpfung max., LO aus) |
| H2 Pluto → RTL-SDR, je Speed | CQ, HB, @ALLCALL 60 Zeichen (Mehrrahmen), gerichtet, SNR? — über CLI, TX-App und Web | 100 % dekodiert (eigener Decoder und Referenz), DT innerhalb ±0,3 s, Frequenzfehler < 1 Hz über den Rahmen |
| H2b Drift Slow | Slow-Rahmen (30 s) mit/ohne Drift-Kompensation, kalt und warm | Mit Kompensation < 0,5 Hz Drift über den Rahmen; sonst `FT8_DRIFT_MODEL` für JS8 nachmessen und dokumentieren |
| H4 Interop mit JS8Call | JS8Call am Loopback dekodiert den RTL-SDR-Empfang unserer Pluto-Aussendung; umgekehrt dekodiert unser RX die von JS8Call erzeugten TX-WAVs aus J0 | JS8Call zeigt Text, Absender und Ziel korrekt; unser RX zeigt JS8Calls Nachrichten korrekt |
| H5 (optional) KW | HackRF ohne RF-Verstärker auf 14,078 bzw. 7,078 MHz (Offset im JS8-Bereich), höchstens 1 mW, einzelne CQ bzw. HB, nie als Dauerserie | Eine echte JS8Call-Station dekodiert uns (Rückmeldung, Spot oder Antwort). Ohne Echo ist das kein Fehler, nur ein Befund. |

Protokolliert werden in `docs/js8/TESTS.md` Datum, Frequenz, Leistung,
Gerät, Inhalt und Zahlen. Die Web-TRX-Doku bekommt einen Abschnitt
„Aktueller Stand“.

### J8 — Abschluss

- `install-js8.sh`:
  - idempotent und nicht als root,
  - gepinnter Commit,
  - Tabellen prüfen,
  - optional `js8`-Binary bzw. apt-Paket,
  - Selbsttest per Python-Import.
- README „Auf einen Blick“ und die Web-TRX-README ergänzen.
- `features.js8.tx` nur, wenn der Codec verfügbar ist.
- Die CI (`.github/workflows/web-trx.yml`) läuft grün. Die reinen
  JS8-Codec-Tests (ohne GNU Radio) kommen in den Job `app-control`.

### J9 — Optional, nur nach eigener Entscheidung des Betreibers

- Automatische Antworten (`SNR?`, `ACK`),
- Heartbeat-Automatik,
- Relay,
- Inbox bzw. Store-and-Forward.

Alles das ist standardmäßig aus, braucht Betreiber-Anwesenheit (Watchdog)
und hat Wiederholungsgrenzen.

---

## 5. Verifikation — Zusammenfassung

- **Ohne Hardware (jede Phase):**
  - pluto-tx `unittest` (inkl. `test_js8_*` und unveränderter FT8-Tests),
  - web-trx `pytest` und `ruff`, `npm run check`,
  - Playwright-Smoke mit dem sim-Backend.
- **Gegen die Referenz:**
  - bitgenaue Vektoren aus dem gepinnten JS8Call (J0/J1),
  - JS8Call-erzeugte WAVs,
  - Off-Air-Mitschnitte mit dem Referenz-Decoder als Maßstab.
- **Hardware (J7):**
  - Pluto sendet, RTL-SDR empfängt, auf 2 m/70 cm,
  - KW nur Empfang mit dem RTL-SDR,
  - optional KW-Senden mit dem HackRF.
  - Abbruchwege und Safe-State werden mit `iio_attr` geprüft,
  - jede Aussendung wird protokolliert.

## 6. Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| Protokolldetail falsch angenommen | J0 belegt alles aus dem Quelltext; bitgenaue Referenzvektoren vor dem ersten Senden |
| Kein offline nutzbarer Referenz-Decoder | Eigener Decoder ist ohnehin Pflicht; Referenz-Decodes dann über JS8Call am Audio-Loopback |
| JSC-Wörterbuch groß / Lizenzfragen | GPLv3 kompatibel; großes Wörterbuch nicht einchecken, zur Laufzeit aus `js8call/` laden |
| Drift des Pluto bei Slow (30 s, 3,125 Hz) | Vorhandene Drift-Kompensation; H2b misst nach |
| Mehrrahmen-Timing (aufeinanderfolgende Slots, Key-Early 3 s) | Ein Rahmen je Tastung wie bei FT8; bei Turbo (6 s) prüfen, ob 3 s Vorlauf plus Umschaltung passen; sonst durchgehend getastet mit einer Quelle je Rahmenfolge (Entscheidung in J3 anhand der Messung) |
| FT8 bricht durch Refactoring | Nur additive Änderungen, Prüfsummentest, volle FT8-Suite in jedem Commit |
| Senden außerhalb der Grenzen | Bandprüfung in `js8_problem()` (ganze belegte Bandbreite), Dämpfungs- bzw. Leistungsgrenze aus Abschnitt 0, Abbruchwege vor den Serien geprüft, begrenzte Serien, Protokoll jeder Aussendung |
