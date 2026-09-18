# Backlog-Plan — weitere LoRa-Modi: Raw → LoRa APRS → LoRaWAN (RX)

Status: geplant, noch NICHT umgesetzt (Nutzer will später starten).

(Meshtastic ist fertig und gepusht: `69796f6`, `3beddb9`; siehe auch `MESHTASTIC_BACKLOG.md`.)

## Context

Meshtastic läuft als Digimode in `pluto-tx`, `pluto-advanced-rx`, `pluto-cli`
(PHY: `pluto_tx/lora.py` + `pluto_advanced_rx/lora_rx.py` um gr-lora_sdr,
Protokoll: `pluto_tx/meshtastic_codec.py`). Der Nutzer will drei weitere
LoRa-Modi, in dieser Reihenfolge: (1) **LoRa Raw** (SF/BW/CR/Sync/Präambel frei,
Hex-Ausgabe; **RX + TX**), (2) **LoRa APRS** (433,775 MHz, Klartext-TNC2, RX+TX),
(3) **LoRaWAN nur RX** (Header/DevAddr/Zähler, kein Senden, keine Entschlüsselung).
Entscheidungen des Nutzers: Raw darf senden; APRS-RX dekodiert Sync 0x12 UND 0x34
parallel; LoRaWAN-Erstwurf = ein Kanal + wählbare SFs. Er wohnt in der Stadt und
erwartet viel Live-Verkehr (Pluto mit hohem RX-Gain ist evtl. besser als der RTL-SDR).

## Wichtige Befunde aus der Vorab-Analyse (gr-lora_sdr Quelltext, read-only)

- **`crc_verif` veröffentlicht JEDEN Frame auf "msg", auch mit falscher CRC**, und
  die Nachricht enthält nur die Nutzdaten (CRC-Bytes und Gültigkeit gehen verloren).
  Folge: Die heutige Meshtastic-RX zeigt CRC-defekte Frames als Müll-Zeilen, und
  Raw könnte "CRC ok/fehlerhaft" nicht anzeigen. → `crc_verif` wird in `LoraRxDecoder`
  durch einen eigenen Python-Block ersetzt.
- `dewhitening` gibt Bytes aus (Nutzdaten + 2 CRC-Bytes, nicht entwürfelt) mit einem
  `frame_info`-Tag (dict: `crc`, `pay_len`, `cr`, `ldro`, …) am Frameanfang;
  `header_decoder` sendet zusätzlich `frame_info` als Nachricht (auch bei Header-Fehler,
  `err=1`). Damit lassen sich Frames samt Metadaten (CR, Länge, CRC vorhanden/ok,
  Header-Fehlerzähler) vollständig selbst zusammensetzen. LoRa-CRC = CRC16 (Poly 0x1021,
  Init 0) über die ersten N-2 Nutzbytes, XOR mit den letzten 2 Nutzbytes, Vergleich mit den
  2 CRC-Bytes (Formel in `gr-lora_sdr/lib/crc_verif_impl.cc` Z.119-145).
- `header_decoder` nimmt `impl_head`, `cr`, `pay_len` (heute hart 255), `has_crc`, `ldro`:
  Implicit-Header braucht also Parameter-Durchreichung (`LoraRxDecoder` setzt heute 255).
- Sync-Wort: ein Byte wird zu (Nibble<<3, Nibble<<3) expandiert; 0x12 → (8,16), 0x34 →
  (24,32) (Standard SX127x/LoRaWAN, funktioniert mit gr-lora_sdr). Die Meshtastic-Anomalie
  (16, 2008) trat nur bei Nibble 0xB (≥ 8) auf → für 0x12/0x34 wird die Nibble-Regel
  erwartet, aber erst der Raw-Modus am Live-Signal beweist es.
- Bereits vorhanden und wiederverwendbar: `lora_airtime.py` (Airtime + `DutyCycleLimiter`),
  `lora_resampler_taps`, `LoraTxEncoder(sf,bw,cr,has_crc,impl_head,ldro,preamb_len,sync_word)`,
  `LoraRxDecoder(...)`, `MeshtasticDeframer`/`MeshtasticState`-Muster, TX-Muster
  (`prepare_*_tx()` → `key_ptt()` → Auto-Unkey), RX-`active_digimode`-Muster, GUI-Gruppen,
  `tests/fakes.py` (Fake-TX/RX-Geräte), `config.in_amateur_band()`.

## Phase 0 — gemeinsame Basis (vor Modus 1)

1. **`pluto_tx/lora_phy.py`** (rein Python): `LoraPhy`-Dataclass (sf, bw_hz, cr 1..4,
   preamble_len, sync (Byte oder 2 Rohsymbole), has_crc, implicit_header, ldro 0/1/2,
   implicit-Payloadlänge) + `label()`, `oversampled_rate()`, `airtime_s(len)`, `sync_symbols()`.
   `lora_airtime.lora_airtime_s` bekommt `ldro`-Parameter. `LoraPreset` bekommt `.phy()`.
   BW-Liste: 7812.5, 10416.7, 15625, 20833.3, 31250, 41666.7, 62500, 125k, 250k, 500k; SF 7–12
   (SF5/6 später).
2. **`LoraFrameSink`** (neuer Python-`sync_block` in `pluto_advanced_rx/lora_rx.py`):
   Eingang dewhitening-Bytestrom + `frame_info`-Tags, berechnet die CRC selbst, Ausgang
   Nachricht "frame" = PMT-Dict {payload(bytes), crc_ok, has_crc, cr, pay_len, ldro}; plus Port
   "info" für Header-Fehler. `LoraRxDecoder` nutzt ihn statt `crc_verif`, bekommt
   `impl_pay_len`. Test: Äquivalenz zu `crc_verif` an gültigen Frames + korrekt „crc_ok=False“
   an manipulierten.
3. **`MeshtasticDeframer` → generischer `LoraFrameDeframer(on_frame)`** (dict statt roher
   Bytes); Meshtastic-RX verwirft `crc_ok=False` (behebt den Müll-Zeilen-Bug).
4. **RX-Flowgraph generisch** (`pluto_advanced_rx/flowgraph.py`): `active_digimode` ∈
   {meshtastic, lora_raw, lora_aprs, lorawan}; ein Helper baut aus einer Liste `LoraPhy`
   je **einen** Resampler pro BW (`lora_resampler_taps`, explizite Taps) + je PHY eine
   `LoraRxDecoder`-Kette, alle → ein `LoraFrameDeframer` (Frame-Dict trägt `phy_label`).
   Meshtastic wird darauf umgestellt (Regressionstests müssen grün bleiben).
5. **TX-Flowgraph generisch** (`pluto_tx/flowgraph.py`): `MODE_LORA_RAW=11`,
   `MODE_LORA_APRS=12` neben `MODE_MESHTASTIC=10`; alle drei teilen `lora_encoder` +
   `lora_tx_resampler` + Producer/`_null_sink_lora` (mehrere Modi → derselbe Producer, kein
   Umstecken beim Wechsel). Encoder wird aus einer `LoraPhy` gebaut; `_lora_phy` /
   `lora_phy_matches()` ersetzt `meshtastic_phy_matches()`; PHY-Wechsel = Flowgraph-Rebuild
   (bestehendes GUI-`_rebuild`-Muster). `_meshtastic_pending` → `_lora_pending`
   (packet, airtime, phy, label); Haltezeit-Formel (Airtime + 10,1 Symbole + Tail) generisch;
   `prepare_lora_raw_tx()` / `prepare_lora_aprs_tx()` analog `prepare_meshtastic_tx()`.

## Phase 1 — LoRa Raw (RX + TX)

- **RX**: GUI-Gruppe (Digimodes-Reiter der RX-App): Felder SF, BW, CR, Sync-Wort
  (Hex-Byte `12`/`34` oder zwei Rohsymbole `16,2008`), Präambel, CRC an/aus,
  Implicit-Header (+ Länge/CR), LDRO (auto/an/aus), Frequenz = vorhandenes `freq_spin`.
  Tabelle: Zeit, Länge, CR, CRC ok, Hex (gekürzt), ASCII; Checkbox „CRC-defekte Frames
  anzeigen“; Zähler Header-Fehler; Übernehmen-Knopf = Rebuild (Muster `_rebuild_for_digimode(force=True)`).
  Zustand `LoraRawState` (Muster `MeshtasticState`, max 500 Zeilen, nur im Speicher).
  Hinweis-Label: Empfang fremder Aussendungen – Inhalte nicht weitergeben.
- **TX**: Digimode „LoRa Raw“ in `pluto-tx`: dieselben PHY-Felder + Hex-Eingabefeld
  (Validierung: gerade Hex-Länge, ≤255 B), Übernehmen = Rebuild, PTT = ein Frame (Auto-Unkey).
  **Schutz**: sendet nur innerhalb `config.DE_AMATEUR_BANDS_HZ`; außerhalb nur mit
  ausdrücklich gesetzter Checkbox „außerhalb Amateurfunkband, eigene Verantwortung“ und dann
  10 % Duty-Cycle (`DutyCycleLimiter`); Label „Amateurband: Rufzeichen-ID nötig, keine
  Verschlüsselung“. Im Amateurband kein Limiter.
- **CLI**: `pluto-cli tx lora-raw --hex … --sf --bw --cr --sync --preamble …`;
  `pluto-cli rx <mode> --digimode lora_raw --lora-*` (JSON-Event `lora_frame`).
- **Zusatz (klein)**: `tools/lora_probe.py` — Offline-Analyse einer IQ-Aufnahme (numpy-Dechirp:
  Präambellänge, Sync-Symbole, CFO), das Verfahren von der Heltec-Messung, damit
  unbekannte Signale vermessen und Parameter bestätigt werden können.

## Phase 2 — LoRa APRS (RX + TX)

- **`pluto_tx/aprs.py`** (rein Python, von beiden Apps genutzt): Frame = `b"<\xff\x01"` +
  TNC2-Text `SRC>DEST,PATH:info`. Builder: Position (Dezimalgrad → `DDMM.mmN/DDDMM.mmE`, Symbol
  Tabelle+Code, Kommentar), Nachricht (`:ADDRESSEE:text{id}`, Adressat auf 9 Zeichen), Status
  (`>`), Roh-Info. Parser: Kopf + Typ (`! = / @` uncompressed+compressed Position, `:` Nachricht,
  `>` Status, `;` Objekt, Mic-E/Wetter/Telemetrie als „nicht dekodiert“, Kommentar erhalten);
  Rufzeichen-/SSID-Validierung; Tocall-Default `APZ…` (experimentell, in `config`, änderbar).
- **Preset** `LoRa APRS (433.775 MHz)`: SF12, BW 125 kHz, CR 4/5, Präambel 8, CRC an, explizit,
  `ham_mode_required=True`, kein Duty-Cycle-Limit; Sync-Wort als Liste (0x12, 0x34).
- **RX** `lora_aprs`: zwei parallele Decoder (Sync 0x12/0x34) hinter EINEM Resampler; Tabelle
  Zeit, Quelle, Ziel, Pfad, Typ, Position/Text, Sync-Wort; Frames mit falscher CRC verworfen.
  Sinnvoll mit Pluto-RX bei hohem Gain (Stadt) — als Hinweis im Tooltip.
- **TX** `lora_aprs`: Felder Rufzeichen(-SSID) (Pflicht), Ziel/Tocall, Pfad, Typ
  (Position/Nachricht/Status/Roh), Sync-Wort (Default nach erster echter Beobachtung mit
  Raw festlegen); PTT = ein Frame; Rufzeichen ist Quelle im Klartext (Amateurfunk-konform, keine
  Verschlüsselung); Label „433 MHz Amateurband“. Kein Auto-Beaconing (bewusst).
- **CLI**: `tx lora-aprs`, `rx … --digimode lora_aprs`.

## Phase 3 — LoRaWAN (nur RX, EU868)

- **`pluto_advanced_rx/lorawan_codec.py`** (rein Python, Header-Parser): MHDR (MType,
  Major), Uplinks/Downlinks: DevAddr (LE), FCtrl (ADR/ACK/FOptsLen), FCnt (16 Bit), FOpts,
  FPort, Payload-Länge, MIC (Hex); Join-Request (JoinEUI/DevEUI/DevNonce sind im Klartext);
  Join-Accept/Rejoin/Proprietary nur als Typ. Plausibilitätsprüfung (Länge ≥ 12, Major 0).
  **Keine Entschlüsselung, keine MIC-Prüfung, kein Senden**, `LoraFrameDeframer` filtert
  `crc_ok`. Getestet mit den Beispielframes der LoRaWAN-Spezifikation.
- **PHY**: Sync 0x34, Präambel 8, BW 125 kHz, CR 4/5, CRC an; EU868-Kanäle (`config`):
  868,1/868,3/868,5 MHz (Standard) + 867,1…867,9 MHz, RX2 869,525 MHz. **Ein Kanal** (Kombo,
  Frequenz = `freq_spin`) + SF7–SF12 einzeln per Checkbox → je gewähltem SF eine
  Decoder-Kette hinter einem Resampler; CPU-Last der 6 Ketten wird gemessen (Ziel: bleibt
  für Pluto/RTL-SDR tragbar; ggf. Standard = SF7–SF9). Downlinks (invertiertes IQ) und
  Mehrkanal (Träger-Offset-Mischer je Kanal) sind bewusst NICHT im ersten Wurf.
- GUI: Tabelle Zeit, SF, Typ, DevAddr, FCnt, FPort, Länge; Zusammenfassung „Geräte“ (DevAddr,
  Anzahl, letzter FCnt). Nur im Speicher, nichts persistiert; README-/GUI-Hinweis: fremde
  Aussendungen, keine Entschlüsselung, Inhalte nicht weitergeben.
- CLI: `rx … --digimode lorawan --lorawan-sf …` (JSON `lorawan_frame`).

## Kritische Dateien

`pluto_tx/{lora.py,lora_airtime.py,config.py,flowgraph.py,gui.py}`, neu `pluto_tx/{lora_phy.py,aprs.py}`;
`pluto_advanced_rx/{lora_rx.py,flowgraph.py,gui.py,config.py}`, neu `pluto_advanced_rx/{lora_frame_deframer.py (Umbenennung
von meshtastic_deframer.py), lora_raw_state.py, aprs_state.py, lorawan_codec.py, lorawan_state.py}`;
`pluto_cli/{tx.py,rx.py,runtime.py,README.md}`, `README.md`, `tests/`, `backlog/meshtastic/` (Notizen).

## Verifikation

- Unit (kein GNU Radio): `LoraPhy`/Airtime, APRS-Builder/Parser (Rundlauf + bekannte
  TNC2-Beispiele), LoRaWAN-Parser (Spezifikations-Beispielframes), CRC-Funktion.
- Software-Loopback (`tests/fakes.py`, Fake-Geräte): Raw mit ungewöhnlichen Parametern
  (SF9/BW62,5k/CR4:7/Sync 0x34, Implicit-Header, ohne CRC, Sync als Rohsymbole), CRC-defekter
  Frame wird als „crc_ok=False“ gemeldet und von Meshtastic/APRS/LoRaWAN verworfen;
  APRS-TX → RX mit beiden Sync-Wörtern; LoRaWAN-artiger Frame (Sync 0x34) aus dem eigenen
  Encoder wird auf dem passenden SF erkannt. Alle 30 bestehenden Tests bleiben grün.
- Offscreen-GUI-Smoke-Tests (TX+RX, wie bei Meshtastic), CLI-Läufe mit Fake-Geräten.
- **Live-RX (ohne Sendefreigabe möglich)**: Pluto/RTL-SDR auf 433,775 MHz (APRS, Sync-Wort
  0x12 vs 0x34 am echten Verkehr klären, Ergebnis in Doku/Default) und 868,1/868,3/868,5 MHz
  (LoRaWAN-Header, SF-Verteilung, CPU-Last).
- **Echte Sendetests nur nach ausdrücklicher Freigabe pro Test** (Projektregel): APRS auf
  433,775 MHz mit Rufzeichen, Raw nur im Amateurband; Endzustand immer per unabhängigem
  `iio`-Read prüfen.

## Reihenfolge / Commits

Phase 0 (Basis + Meshtastic-Regression) → Phase 1 → Phase 2 → Phase 3; nach jeder Phase
Tests grün, README/Backlog aktualisieren, danach Commit+Push nur auf Zuruf des Nutzers.

---
