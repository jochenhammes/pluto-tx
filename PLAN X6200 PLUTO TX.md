# Plan: Xiegu X6200 (OpenX6X00) als externes Gerät in pluto-tx

Stand: 2026-11-02 · Status: **Phasen 0–5 weitgehend umgesetzt, Stand und Abweichungen direkt unter dieser Zeile**

> **Stand der Umsetzung, Ergebnisse statt Annahmen.** Messwerte: `docs/x6200/TESTS.md`, Aufbau: `docs/x6200/SPEC.md`.
> - **A1 bestätigt:** Soundkarte und zwei serielle Ports (`/dev/ttyACM*`, einer spricht CI-V). **A2 entschieden:** USB-Audio sendet nur in USB-D und LSB-D. Damit entfallen FM, POCSAG und APRS über dieses Kabel. **A3:** 48 kHz, 16 Bit; Empfangsaudio liegt unabhängig von der Lautstärke an.
> - **Hamlib:** Ubuntu 4.6.5 kennt Modell 3091.
> - **Abweichungen vom Plan:**
>   - `is_audio_only()` bedeutet jetzt „Signal über den Audioweg" und bleibt beim X6200 `True`; dazu kommt `has_frequency_control()`. Nur die Frequenz- und Bandstellen wurden angepasst, nicht alle rund 40 Aufrufe.
>   - Neu ist `pluto_tx/rig_guard.py`: Gemessen bleibt der Sender nach dem Tod des Hosts getastet; ein eigener Prozess löst die PTT bei Zeitablauf und beim Schließen der Pipe.
>   - `rigctld` wird nie automatisch beendet, weil Wächter und andere Apps ihn brauchen können.
>   - Messgegenstelle ist der HackRF mit Whip-Antenne, kein Abgriff am Dummy-Load.
> - **Umgesetzt:** Phasen 1–3, Phase 4 für SSB und LSB (FreeDV nicht), Phase 5 für GUI, CLI, Web-TRX, Installation, Phase 6 für CLI und Web-TRX-Backend.
> - **Entscheidungen:** TCI nur als Rückfallebene, nicht gebaut. 10 % Standardgrenze, 120 s Sendelimit im Wächter. Gerätename `x6200`.
> - **Offen:** siehe „Offene Punkte" in `docs/x6200/TESTS.md`, dazu Betrieb an der Antenne (Phase 8) und das Ziehen des USB-Kabels während einer Sendung.
Geprüft gegen: `jochenhammes/pluto-tx` @ `a66593e` und `Guru-RF/OpenX6X00` @ `e475fbb` (Doku nennt v0.3.8), Hamlib `master`.
Ziel-Branch (später): `feature/x6200-device` in pluto-tx.

---

## 0. Kurzfazit

- **Die Idee trägt.** USB-Soundkarte + Hamlib-CAT ist für den X6200 plausibel, und pluto-tx hat mit `SoundcardDevice` / `AiocDevice` bereits fast das ganze Muster (Audio-Sink + echte PTT-Hardware über `needs_ptt_control`).
- **Der X6200 ist aber kein reines Audio-Gerät.** Er hat Frequenz, Modus, Leistung, Meter. pluto-tx nimmt bisher an: „Audio-only = keine Frequenz" (`is_audio_only()` ⇔ `frequency_range_hz == (0, 0)`). Dieselbe Eigenschaft steuert dort zwei verschiedene Dinge (Audio-Zweig statt SDR-Sink *und* RF-Controls/Bandprüfung ausblenden). Das muss für den X6200 aufgetrennt werden. Das ist der größte Eingriff (Phase 2).
- **„Alle pluto-tx-Funktionen" sind physikalisch nicht erreichbar.** Ein SSB-/FM-Transceiver mit ~3 kHz Audiopfad kann kein LoRa, kein 23-cm-File-Broadcast, kein Baseband/IQ, kein ADS-B/ADS-L. Erreichbar sind alle Audio-/Tonmodi (FM-Sprache fraglich, siehe Modus-Matrix) und die SSB-tauglichen Digitalsprach-Modi. Realistisches Ziel: **alles, was ein KW-/6-m-Transceiver tragen kann.**
- **Drei Dinge sind belegt, drei sind Annahmen** (Abschnitt 1). Die Annahmen entscheiden, ob der USB-Audio-Weg in allen Modi funktioniert. Deshalb beginnt der Plan mit einer **Hardware-Inventur am echten Gerät (Phase 0)**, bevor Code entsteht.
- **Rückfallebene mit echtem Mehrwert:** Der TCI-Server der Firmware (Port 50001) trägt CAT *und* Audio in beide Richtungen über das Netz, laut Doku auch in NFM und AM. Wenn USB-Audio in manchen Modi nicht sendet, löst TCI das (Phase 7, optional).

---

## 1. Ausgangslage: was belegt ist und was nicht

### 1.1 Belegt (Quelltext / Doku gelesen)

**pluto-tx**
- Geräteschicht: `pluto_tx/devices/{base,soundcard,aioc,pluto,hackrf,espdr}.py`, Registry in `devices/__init__.py`. Neues Gerät = neue Klasse + Registry-Eintrag.
- `TxDevice` hat die nötigen Hooks: `pre_key()`, `post_unkey()`, `force_safe_state()`, `read_hw_state()`, `audio_sink_device`, `needs_ptt_control`, `supported_modes`, `probe_with_timeout()`, `scan_devices_with_timeout()`.
- Audio-Zweige existieren für: FM-Audio, RADE, Digitext (Waterfall Writer), PSK31, RTTY, POCSAG, FT8, JS8, APRS, JTTY (`flowgraph.py`, Verdrahtung über `_soundcard_audio_producer_map()`, ein gemeinsamer `audio.sink`).
- **Nicht** als Audio-Zweig vorhanden: SSB/LSB-Sprache, M17, FreeDV (die laufen heute nur als IQ zum SDR-Sink).
- GUI hält die Liste `_AUDIO_ONLY_CAPABLE_MODES` (FM, RADE, DIGITEXT, PSK31, RTTY, POCSAG, FT8, JS8, APRS). **JTTY fehlt dort**, obwohl `flowgraph.py` einen JTTY-Audio-Zweig hat → prüfen, vermutlich Mini-Fix.
- Leistungsregler: `power_slider` ist ein ganzzahliger `QSlider` von `stage.min_value` bis Ceiling, Beschriftung aus `stage.label`/`stage.unit`. Eine Watt-Stufe 0–8 W wäre damit zu grob.
- `in_amateur_band()` wird im Flowgraph nur für **nicht**-Audio-only-Geräte geprüft (`flowgraph.py` ~1512, ~1554) und im CLI (`app.py:45`).
- RX-Seite: `pluto_advanced_rx/devices/audio.py` (`AudioDevice`, 48 kHz, real → complex) existiert.
- Tests: `unittest`, Fakes in `tests/fakes.py` (`FakeAudioTxDevice`), Vorbild `tests/test_aioc.py` (Fake-Serial, kein Gerät nötig). CI (`web-trx.yml`) hat kein GNU Radio, GNU-Radio-Tests skippen dort.

**OpenX6X00 (X6100 *und* X6200 unterstützt)**
- **CAT über USB:** Die Linux-Seite öffnet `/dev/ttyS2` („CAT serial port behind the USB bridge", `board.h`), 19200 8N1, **CI-V-Adresse 0xA4**. `HW_PIN_USB_CAT` (gesetzt über `radio_usb_to_cat()`) schaltet „the USB port's serial lines to the CAT port" (GPIO). Der USB-Seriell-Chip ist also Hardware; die Firmware spricht nur den UART.
- **`btcat`** legt ein PTY über `/dev/ttyS2` und multiplext USB + bis zu 3 Bluetooth-Clients: **eine Anfrage zur Zeit, 300 ms Timeout pro Anfrage**, Transceive-Frames (dst=0x00) gehen an alle Clients.
- **CI-V-Befehlssatz** (Referenztabelle in `docs/MANUAL.md`): Frequenz (`03`/`05`/`25`), Modus (`04`/`06`/`26`; LSB, USB, AM, CW, NFM, CW-R), Datenmodus (`1A 06`, USB↔USB-D, LSB↔LSB-D), **PTT `1C 00`**, Leistung (`14 0A`, 0–255 = 0–10 W, X6200 stoppt bei 8 W), Meter (`15 02` S, `15 11` Leistung, `15 12` SWR, `15 13` ALC), Geräte-ID `19 00` → `A4`. Alles andere → `FA`.
- **CAT-PTT** (`1C 00 01`) ruft `radio_set_ptt(true)` → `HW_PTT`. Die Firmware verweigert den Sendebetrieb auf nicht-sendefähigen Frequenzen (`tx_refused()`).
- **Hamlib:** `rigs/icom/xiegu.c` hat `X6200` (`RIG_MODEL_X6200` = **3091**, Adresse 0xA4, 300–19200 Baud 8N1, `write_delay = 3`, Datenmodus unterstützt, PTT über `x108g_set_ptt`, Version `.1`, Status stable). Der X6100 ist **3087**. Die OpenX6X00-Doku nennt für Hamlib/flrig „rig model Xiegu X6100".
- **TCI 2.0-Server** (Port 50001, standardmäßig an, kein Login): Frequenz, Modus, Filter, PTT (`trx:0,true,tci;`), Meter und Audio beidseitig. RX-Audio 24/48 kHz, int16/int24/int32/float32; TX-Audio per `TX_CHRONO` alle 21,3 ms; **TCI-TX-Audio in jedem Modus außer CW/CW-R** (also auch NFM/AM).
- **Die „USB-Soundkarte"** wird in der Doku genannt („the radio's USB sound card and CAT"). In den Quellen taucht sie nur als Hardware der Hauptplatine auf (`recover_processing_audio_inputs()`: „main board and USB soundcard", nur X6100-Build). Die Linux-Seite stellt sie **nicht** bereit.

### 1.2 Annahmen (Phase 0 muss sie klären)

| # | Annahme | Warum es zählt |
|---|---|---|
| A1 | Der USB-C-Port am X6200 zeigt am PC **eine Audio-Klasse-Soundkarte und eine serielle Schnittstelle** (Windows braucht laut Beschreibung des RSGB-Guides zum X6200 einen Treiber für das USB-Kabel → vermutlich USB-Seriell-Chip, unter Linux CH340/CP210x/…; nicht selbst nachgeprüft) | Ohne beides gibt es keinen Weg über das Kabel |
| A2 | **Welche Modi das USB-Audio als TX-Quelle nehmen.** Bei Xiegu-Geräten ist das üblicherweise die Daten-Variante (USB-D/LSB-D); in NFM/AM/normalem USB könnte die Mikrofonquelle gelten | Entscheidet, ob FM/SSB-Sprache/POCSAG über USB-Audio gehen |
| A3 | USB-Audio-Format (Abtastrate 48 kHz?, mono/stereo?, 16 Bit?) und ob RX-Audio auf dem USB-Capture in **allen** Modi anliegt (und vor/nach der Lautstärke) | Pegelkalibrierung, `plughw`-Wahl, RX-Gerät |
| A4 | Bezeichnung „DEV-Port": In den gelesenen Quellen steht nur „USB cable" bzw. „HOST port" (USB-A, WR12/Zubehör). Ich gehe davon aus, dass dein „Dev-Port" der PC-seitige USB-C-Anschluss ist | Falls nicht: anderer Port, anderer Plan |

---

## 2. Plausibilitätsprüfung

| Anforderung | Urteil | Begründung / Vorbehalt |
|---|---|---|
| Soundkarten-Interface | **plausibel** | Muster existiert (`SoundcardDevice`, `AiocDevice`). Vorbehalt A2/A3. |
| Hamlib-CAT | **plausibel, belegt** | X6200 = Modell 3091 in Hamlib; CI-V-Tabelle der Firmware deckt Frequenz/Modus/PTT/Leistung/Meter ab. Fallback Modell 3087. |
| CAT-Zugriff von pluto-tx **und** anderen Programmen | **nur mit gemeinsamem Besitzer** | Serielle Schnittstelle ist exklusiv. Lösung: `rigctld` als einziger Port-Besitzer, pluto-tx spricht TCP. |
| Modus-Wechsel/Datenmodus per CAT | **plausibel** | `M PKTUSB 0` → `1A 06`. Nicht per CAT erreichbar: SAM, WFM (X6200-AM-Taste). |
| Einbau in den bestehenden Flowgraph | **machbar, aber mit Refactor** | `is_audio_only()` trennt nicht zwischen „Audio-Pfad" und „keine RF-Steuerung" (Phase 2). |
| Sichere PTT/E-STOP | **machbar, mit Zusatzmaßnahmen** | CAT-PTT hängt an einer seriellen Verbindung, kein Dead-Man-Draht. Abschnitt 6. |
| „Alle pluto-tx-Funktionen" | **nein, siehe Matrix** | Physikalische Grenzen, nicht Software. |

---

## 3. Modus-Matrix

Legende: ✔ vorhanden/klar machbar · ◐ machbar mit neuem Zweig oder offener Frage · ✘ mit diesem Gerät nicht sinnvoll

### TX (`pluto-tx`)

| Modus | Urteil | Bemerkung |
|---|---|---|
| FM (NFM, optional CTCSS/DCS) | ◐ | FM-Audio-Zweig existiert. Ob NFM das USB-Audio nimmt: A2. Sonst nur über TCI (Phase 7). Nur 10 m/6 m sinnvoll. |
| SSB (USB/LSB) Sprache | ◐ | Neuer Audio-Zweig nötig (Mic/Datei → Bandpass ~300–2700 Hz → Sink). Kein IQ. A2 beachten. |
| M17 | ✘ (später ◐) | 4FSK mit 9 kHz Bandbreite; SSB-Pfad geht nicht, NFM-Pfad mit Mikrofon-Vorverzerrung/Begrenzung unsicher. Höchstens experimentell über TCI. |
| FreeDV 2020/2020B | ◐ | Schmalband-OFDM, für SSB gedacht. Braucht neuen Audio-Zweig; Ausgabeform von `freedv_tx()` im Code prüfen. |
| RADE V1 | ✔ | Audio-Zweig existiert (`rade_audio_*`). Voraussetzung: sauber lineares TX-Audio (ALC/Prozessor aus, A2). |
| Waterfall Writer, PSK31, RTTY | ✔ | Audio-Zweige existieren. |
| FT8, JS8, JTTY | ✔ | Audio-Zweige existieren (JTTY: GUI-Liste ergänzen). Slot-Timing: PTT-Latenz kompensieren (Phase 0 misst sie). |
| POCSAG, APRS | ◐ | Technisch als Töne sendbar, aber 1200-Bd-AFSK ist ein UKW-FM-Verfahren. Auf KW nur Sondernutzung; auf 10 m/6 m bräuchte man FM → A2/TCI. |
| File Broadcast | ✘ | 23-cm-Verfahren; der X6200 hat kein 23 cm. |
| Baseband (IQ) | ✘ | Kein IQ über USB-Audio. |
| Meshtastic, MeshCore (LoRa) | ✘ | Chirp 125 kHz+, 433/868 MHz. |

### RX (`pluto-advanced-rx`, Phase 6)

| Modus | Urteil |
|---|---|
| SSB, RADE, FT8, JS8, JTTY, PSK31, RTTY, Waterfall Writer | ✔ über USB-Audio-Capture (`AudioDevice` + CAT-Abstimmung) |
| FM, POCSAG, APRS | ◐ (A3: Audio im FM-Modus anliegend?) |
| M17, Baseband, ADS-B, ADS-L, LoRa | ✘ |

Von den 18 TX-Modi des Flowgraphs (SSB und LSB zählen einzeln) bleiben damit 10 voll oder mit überschaubarem Aufwand (7 vorhanden, SSB, LSB und FreeDV mit neuem Zweig), 3 offen (FM, POCSAG, APRS hängen an A2) und 5 ausgeschlossen (M17, File Broadcast, Baseband, Meshtastic, MeshCore). Die genaue Zahl liefert Phase 0.

---

## 4. Zielarchitektur

### 4.1 Neue und geänderte Dateien

| Datei | Zweck |
|---|---|
| `pluto_tx/rigctl.py` (neu) | Reiner Python-Client für das rigctld-Textprotokoll (Socket, keine Hamlib-Python-Bindings nötig) + `RigctldManager` (verbinden oder eigenen `rigctld` starten) |
| `pluto_tx/devices/x6200.py` (neu) | `X6200Device(TxDevice)`: Audio-Sink (`audio_sink_device`), CAT-PTT, Frequenz, Modus, Leistung, Meter |
| `pluto_tx/devices/base.py` | Neue Fähigkeits-Flags (4.3), `PowerStage`-Art für Prozent/Watt |
| `pluto_tx/devices/__init__.py` | Registry `"x6200"`, defensiver Import wie bei AIOC |
| `pluto_tx/flowgraph.py` | `is_audio_only()`-Aufrufe aufteilen (Phase 2), neue Zweige SSB-Audio/FreeDV-Audio (Phase 4) |
| `pluto_tx/gui.py` | `connection_kind="rig"`, Frequenz-/Modus-/Meter-Anzeige für Audio-Geräte mit RF-Steuerung, Leistungsregler-Auflösung |
| `pluto_cli/tx.py`, `runtime.py`, `devices_cmd.py` | `--device x6200`, Scan/Probe |
| `pluto_advanced_rx/devices/x6200.py` (neu, Phase 6) | `AudioDevice`-Ableger mit CAT-Abstimmung und S-Meter |
| `web-trx/backend/web_trx/radio_backend.py` | Gerätetyp zulassen, Leistungsstufe darstellen |
| `pluto_tx/doctor.py`, `install.sh` | Prüfung auf `rigctld`, USB-Geräte, Gruppen (`dialout`, `audio`) |
| `tests/fake_rigctld.py`, `tests/test_rigctl.py`, `tests/test_x6200.py` (neu) | Siehe Abschnitt 7 |
| `docs/x6200/SPEC.md`, `docs/x6200/TESTS.md` (neu) | Dokumentation im Stil von `docs/jtty/` |

### 4.2 Verbindungszeichenfolge

Analog zu AIOC: `<rig>|<alsa>`

- `<rig>`: entweder `rigctld://host:port` (vorhandenen rigctld benutzen) oder ein serieller Port wie `/dev/ttyUSB0` (pluto-tx startet dann `rigctld -m 3091 -r <port> -s 19200 -t <freier Port>` auf `127.0.0.1` und besitzt ihn). Modell überschreibbar (`…@3087`).
- `<alsa>`: z. B. `plughw:CARD=<Name>,DEV=0`. Der echte Kartenname steht erst nach Phase 0 fest.
- Leer = Auto-Scan (serielle Schnittstelle per USB-ID + ALSA-Karte, gleiche Logik wie `AiocDevice.scan_devices_with_timeout()`; bei Mehrdeutigkeit Kandidaten listen).

Ein gemeinsamer rigctld ist der Grund für die TCP-Variante: `pluto-tx`, `pluto-advanced-rx` und `web-trx` laufen als getrennte Prozesse und müssen sich denselben CAT-Port teilen, und die Firmware selbst arbeitet nur eine Anfrage zur Zeit ab.

### 4.3 Der Refactor in `base.py`

Heute: `is_audio_only()` = „keine Frequenz". Gebraucht werden zwei getrennte Eigenschaften:

- `uses_audio_path` — Signal geht über den gemeinsamen `audio.sink` statt über den SDR-Sink (steuert alle Audio-Zweige in `key_ptt()`/`unkey_ptt()`/`__init__`).
- `has_frequency_control` — das Gerät hat eine abstimmbare Frequenz (steuert Frequenzfeld, `in_amateur_band()`-Prüfung, Retune-Aufrufe).

Bestehende Geräte behalten ihr Verhalten: Soundcard/AIOC = `uses_audio_path=True, has_frequency_control=False`; Pluto/HackRF/ESP32 umgekehrt; **X6200 = beides `True`**. `is_audio_only()` bleibt als Alias für `uses_audio_path and not has_frequency_control`, damit Aufrufer außerhalb des Flowgraphs nicht brechen. Ungefähr 30 Aufrufstellen in `flowgraph.py` und `gui.py` werden einzeln entschieden (Audio-Zweig → `uses_audio_path`, Frequenz/Band → `has_frequency_control`). Absicherung durch die bestehenden Tests (`test_aioc`, `test_fm_voice`, `test_espdr_device`, `test_repeat` …) vor und nach dem Refactor, im Commit davor als Baseline gespeichert.

### 4.4 Leistungsstufe

Primärstufe `"rfpower"`, Hamlib-Level `RFPOWER` (0.0–1.0, Anteil der Maximalleistung). Bevorzugt als Prozent-Stufe (`kind="continuous_pct"`, 0–100 %, 100 % = 8 W bzw. 5 W im Akkubetrieb laut Firmware-Doku). Grund: der GUI-Slider ist ganzzahlig, ein Watt-Raster wäre zu grob. Konvention „mehr Wert = mehr HF" bleibt erhalten. Wattangabe in `read_hw_state()` über `power2mW`.

`default_power_ceiling`: niedrig (Vorschlag 10 %), volle Leistung nur über „Unlock full power", wie bei den SDR-Geräten.

### 4.5 CAT-Zugriffsregeln (wegen `btcat`)

- Nur Anfrage/Antwort, **keine parallelen Pollschleifen**. Meter höchstens 2–5 Hz (Firmware-Timeout 300 ms pro Anfrage).
- Keine CAT-Zugriffe in der Audio-Callback-Kette; alles in einem eigenen Thread mit Lock.
- Vor dem Unmuten des Audios: `T 1` senden, `t` zurücklesen, erst dann Pegel hoch (siehe Abschnitt 6).
- Transceive-Frames (unaufgeforderte `00`/`01`-Frames) können Hamlib-Antworten verschränken. In Phase 0 mit `rigctl -vvvvv` prüfen, ggf. Transceive abschalten oder über rigctld-Optionen entschärfen.

---

## 5. Phasen

Jede Phase endet mit einem Commit auf `feature/x6200-device` und grünen Tests. Phasen 1–3 brauchen **keine** Hardware.

### Phase 0 — Hardware-Inventur (am echten X6200, Dummy-Load, ≤ 1 W)

Ergebnis: ausgefüllte Messtabelle in `docs/x6200/TESTS.md`, Annahmen A1–A4 entschieden. Ein Hilfsskript `tools/x6200_probe.py` automatisiert, was automatisierbar ist.

1. Enumeration: `lsusb -v`, `dmesg`, `udevadm info`, `arecord -l`, `aplay -l`, `arecord --dump-hw-params` (Raten, Kanäle, Format), seriellen Port finden. → A1, A3, A4
2. CAT: `rigctl -m 3091 -r <port> -s 19200 f m t` und `-vvvvv`; Fallback `-m 3087`. `19 00` muss `A4` liefern. Transceive-Verhalten beobachten.
3. Exklusivität: Zweiten Prozess auf den Port, parallel Bluetooth-CAT, falls vorhanden.
4. Modus-Routing (A2): Je Modus (USB, USB-D, LSB-D, NFM, AM) 1-kHz-Ton über USB-Audio senden, Empfang mit Pluto/HackRF/RTL-SDR am Dummy-Load-Abgriff oder per Wattmeter/Meter `15 11`. Ergebnis je Modus: sendet ja/nein, Pegelverhältnis.
5. RX-Audio: Pegel, Rate, Kanal; liegt Audio in allen Modi und bei Lautstärke 0 an?
6. Timing: Zeit von `T 1` bis HF (SDR-Aufzeichnung), von Audio-Start bis HF, von `T 0` bis HF-Ende, CAT-Roundtrip. **Nur messen, keine Zahlen vorab annehmen.**
7. Pegel/ALC: Audiopegel (dBFS) → ALC-Anzeige (`15 13`) und Ausgangsleistung; Pegel finden, bei dem ALC nicht anspricht.
8. Sicherheitsverhalten: Was passiert mit der Sendung, wenn der Host stirbt (`kill -9`), das USB-Kabel gezogen wird oder `rigctld` abgebrochen wird? Hat der Sender eine eigene Sendezeitbegrenzung?
9. Datenmodus-Umschaltung über CAT: `M PKTUSB 0` → Anzeige am Gerät.

Abbruchkriterium: Sendet USB-Audio in USB-D nicht, entfällt der Hauptweg; dann zuerst Phase 7 (TCI) statt Phase 3–4.

### Phase 1 — Hamlib-Client (`pluto_tx/rigctl.py`)

- Protokoll: `f`/`F`, `m`/`M`, `t`/`T`, `l`/`L` (`RFPOWER`, `STRENGTH`, `SWR`, `ALC`, `RFPOWER_METER`), `\power2mW`, `\get_info`. `RPRT n` auswerten; Hamlib-Fehlercodes → eigene Ausnahmen (Timeout, `-9` I/O, `-11` Modus/Freq nicht erlaubt …).
- `RigctldManager`: verbinden / eigenen Prozess starten und beenden; Wiederverbindung; Heartbeat; sauberes `close()`.
- `tests/fake_rigctld.py`: kleiner TCP-Server, der das Protokoll spricht, Zustand hält und Fehler/Verzögerungen/Abbrüche einspielen kann.
- Tests (reines Python, laufen in der CI ohne GNU Radio): Roundtrips, Fehlerpfade, Timeout, Abbruch mitten im Satz, Reconnect, gleichzeitige Aufrufer.

### Phase 2 — Fähigkeits-Flags (Refactor, kein neues Verhalten)

- Abschnitt 4.3 umsetzen, Baseline-Tests vorher und nachher.
- Prüfen/ergänzen: JTTY in `_AUDIO_ONLY_CAPABLE_MODES`.
- Abnahme: gesamte bestehende Suite grün, kein Verhaltensunterschied für Pluto/HackRF/Soundcard/AIOC/ESP32.

### Phase 3 — `X6200Device` (TX, nur Audio-Zweige, die es schon gibt)

- Klasse nach `AiocDevice`-Vorbild: `prepare_for_start()` öffnet CAT, liest Zustand, setzt sichere Leistung; `pre_key()` = Lead-in + CAT-PTT mit Rücklesen; `post_unkey()` = Tail-out + PTT aus; `force_safe_state()` = nicht blockierend, idempotent, wirft nie (PTT aus, Leistung auf Minimum).
- `set_frequency()` (Dial), `set_mode()` (Datenmodus), `set_power()`, `read_hw_state()` (Leistung, SWR, ALC, S-Meter, PTT).
- Frequenz-Konvention: Dial-Frequenz in USB-D, Audio-Träger in Modus-Mitte (wie bei JTTY heute: „`--freq` = USB-Dial"). In `docs/x6200/SPEC.md` festhalten, damit FT8-/RADE-Offsets eindeutig sind.
- Bandprüfung greift (`has_frequency_control=True`).
- Tests (`tests/test_x6200.py`, Fake-rigctld + `FakeAudio`): Reihenfolge PTT/Audio, Fehler bei `RPRT -11`, `force_safe_state` unter Last, Reconnect während Sendung, kein Unmute bei verweigerter PTT.
- Abnahme am Gerät (Dummy-Load): FT8-Burst, PSK31, RADE — jeweils mit Gegenstation/SDR dekodiert.

### Phase 4 — fehlende Zweige

- **SSB-Audio** (Mic/Datei → Bandpass → Sink), **FreeDV-Audio** (nach Prüfung der `freedv_tx()`-Ausgabe), je mit Tests wie die bestehenden Audio-Zweige.
- Je nach Phase-0-Ergebnis: FM/POCSAG/APRS freischalten (A2) oder für den X6200 sperren (`supported_modes`, GUI grau mit Hinweis, CLI-Fehlermeldung — der Mechanismus existiert schon vom ESP32).

### Phase 5 — GUI, CLI, web-trx, Installation

- GUI: Verbindungsfeld `rig`, Frequenz/Modus/Meter sichtbar, Leistungsregler in Prozent, Hinweis zu Pegel/ALC.
- CLI: `--device x6200 --uri '<rig>|<alsa>'`, `pluto-cli devices scan --tx --device x6200`, JSON-Ereignisse wie bei den anderen Geräten.
- web-trx: Gerätetyp zulassen, Meter-Anzeige.
- `install.sh`/`doctor.py`: `libhamlib-utils` (rigctld), Gruppen, udev-Regel für stabilen Gerätenamen.
- Doku: README-Abschnitt, `docs/x6200/*`.

### Phase 6 — RX-Gerät (`pluto-advanced-rx`)

- `X6200RxDevice` auf Basis von `AudioDevice`: Abstimmung per CAT, S-Meter, Audio-Capture. Spektrum/Wasserfall nur über das Audio (kein IQ).
- Gemeinsamer rigctld mit dem TX-Gerät; Abstimmung nie während einer Sendung.

### Phase 7 — optional: TCI-Transport

- `transport = usb | tci` im selben Gerät. TCI per WebSocket (`websockets`-Paket oder kleiner eigener Client): `vfo`, `modulation`, `drive`, `trx:0,true,tci;`, `TX_CHRONO`-Takt, Meter.
- Vorteile: Audio auch in NFM/AM, kein Kabel, kein ALSA. Nachteile: Netzwerk, **kein Login** (Firmware-Doku rät zu vertrauenswürdigem Netz), Audio ~3 Mbit/s, Taktbindung an `TX_CHRONO`.
- Wird vorgezogen, wenn Phase 0 zeigt, dass USB-Audio in wichtigen Modi nicht sendet.

### Phase 8 — Funktests und Release

- Betriebstest an der Antenne (nur mit Lizenz, Band/Leistung nach Bandplan), Eintrag in die Release-Notes mit klar benannten **nicht getesteten** Teilen, wie es das Projekt bisher handhabt (`docs/release/*`).

---

## 6. Sicherheitskonzept (TX-kritisch)

pluto-tx legt besonderen Wert auf eine von GNU Radio unabhängige Abschaltlogik (`safety.py`). Beim X6200 gilt dasselbe Prinzip:

1. **Reihenfolge Tasten:** Leistung auf sicheren Wert → CAT-PTT an → `t` zurücklesen = TX → erst dann Audio entmuten. Verweigert die Firmware den Sendebetrieb (`tx_refused()`, falsche Frequenz), bleibt das Audio stumm und der Aufrufer bekommt einen Fehler, **bevor** HF entsteht.
2. **Reihenfolge Loslassen:** Audio stumm → Tail-out → PTT aus → zurücklesen; bei Fehlern wiederholen.
3. **`force_safe_state()`** unabhängig vom Flowgraph (eigener Thread/Socket, vorhandene Muster aus `PlutoSafety`/`AiocPtt.force_release()`), eingehängt in dieselben Pfade (E-STOP, Fenster schließen, SIGINT/SIGTERM, `atexit`, Ausnahmen).
4. **Sendezeitbegrenzung im Host:** Maximale Sendedauer pro Taste (Standard konservativ), danach automatisch PTT aus.
5. **Totmann-Prüfung:** Bricht die CAT-Verbindung während einer Sendung ab, versucht der Host mehrfach, die PTT zu lösen, und meldet es laut. Ob die Funk-Seite dabei weitersendet, misst Phase 0 (Punkt 8); das Ergebnis steht dann in der Doku als bekannte Einschränkung.
6. **SWR-/Leistungsüberwachung:** Meterwerte aus `read_hw_state()`; hohes SWR → Sendung beenden und melden (die Firmware hat eigenen SWR-Schutz, wird nicht ersetzt, nur ergänzt).
7. **Pegel:** Startpegel niedrig, ALC-Kalibrierung aus Phase 0, kein Digitalmodus über dem ALC-Einsatz. Prozessor/Kompressor der Gegenstelle möglichst aus (nur über Menü des Geräts steuerbar, in der Doku aufführen).
8. **Bandprüfung** bleibt aktiv (`in_amateur_band()`), die Frequenz kommt immer aus dem Gerät zurück und wird gegen die Anforderung geprüft.

---

## 7. Teststrategie

**Ebene 1 — Unit, ohne Hardware, in der CI** (GitHub Actions, ohne GNU Radio): `test_rigctl.py` mit Fake-rigctld (Protokoll, Fehler, Timeouts, Abbrüche, Reconnect). Als Job neben `app-control` in `web-trx.yml` eintragbar.

**Ebene 2 — Flowgraph mit Fakes** (auf dem Funkserver mit GNU Radio): `test_x6200.py` mit `FakeRig` + Null-Audio; prüft Reihenfolge PTT/Audio, `needs_ptt_control`-Verhalten, alle Audio-Zweige mit dem neuen Gerät, Sperrlisten (`supported_modes`), Refactor-Regression (`test_aioc`, `test_fm_voice`, `test_espdr_device`, `test_repeat`, …).

**Ebene 3 — Bank-Test am Gerät, Dummy-Load** (Protokoll in `docs/x6200/TESTS.md`, Ergebnisfeld je Zeile):

| ID | Test | Erwartung |
|---|---|---|
| H-CAT-1 | `f`/`m`/`t` lesen | plausible Werte, kein Timeout |
| H-CAT-2 | `F 14074000`, `M PKTUSB 0` | Anzeige am Gerät folgt |
| H-CAT-3 | `T 1`/`T 0` im Takt | Sender tastet, Rücklesen stimmt |
| H-CAT-4 | Meter während TX (2–5 Hz) | Werte plausibel, keine Timeouts |
| H-AUD-1 | RX-Audio aufnehmen | Rate/Kanäle laut Phase 0, Pegel sinnvoll |
| H-AUD-2 | 1-kHz-Ton in USB-D | HF am SDR-Abgriff |
| H-AUD-3 | dasselbe in USB, NFM, AM | Matrix A2 ausgefüllt |
| H-AUD-4 | Pegel-Sweep | ALC-Grenze, lineares Verhalten bis dahin |
| H-TIM-1 | PTT→HF, Audio→HF, Ende | Zahlen für Lead-in/Tail-out |
| H-SAF-1 | `kill -9` während Sendung | Verhalten dokumentiert |
| H-SAF-2 | USB-Kabel ziehen | Verhalten dokumentiert |
| H-SAF-3 | E-STOP | Sender in < Tail-out dunkel |
| H-MOD-n | je Modus: FT8, JS8, PSK31, RTTY, RADE, Waterfall Writer, JTTY, SSB, (FreeDV, FM, POCSAG, APRS) | Gegenstelle/SDR dekodiert, Protokoll |
| H-RX-1 | RX-Gerät in pluto-advanced-rx | FT8/RADE/SSB dekodiert, Abstimmung per CAT |
| H-MULTI-1 | TX-App + RX-App + gemeinsamer rigctld | kein CAT-Konflikt, keine Aussetzer |

**Ebene 4 — Betrieb auf Band:** Checkliste mit Lizenz-/Bandplanhinweis, geringe Leistung, Beobachtung per WebSDR/RBN/PSKReporter, Ergebnisse in die Release-Notes.

---

## 8. Branch- und Arbeitsablauf

1. Branch `feature/x6200-device` von `main` (aktuell `a66593e`).
2. Reihenfolge der Commits: Baseline der Tests → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7/8. Phase 0 liefert nur Messdaten und das Probe-Skript.
3. Voraussetzungen für die Umsetzung: **Schreibzugriff auf `jochenhammes/pluto-tx`** (in dieser Sitzung ist nur Lesezugriff angebunden) und **Zugang zum X6200** für Phase 0/3/4/8. Phasen 1 und 2 lassen sich ohne Gerät vorbereiten.
4. `espdr/` darf nicht angefasst werden (Kopie aus eigenem Upstream, siehe README) — hier irrelevant, aber Muster für „eigenständiges Teilpaket mit Sync-Skript", falls ein CI-V-Direktzugriff später ausgelagert werden soll.
5. Falls Phase 0 zeigt, dass eine Firmware-Änderung nötig wäre (z. B. Transceive abschalten, Audio-Routing), gehört das in den OpenX6X00-Fork; pluto-tx bekommt dann nur eine Mindestversion in der Doku.

---

## 9. Risiken und offene Fragen

| Risiko | Wirkung | Gegenmaßnahme |
|---|---|---|
| USB-Audio ist nicht in allen Modi TX-Quelle (A2) | FM/SSB-Sprache/POCSAG/APRS nicht über USB | Phase 0; TCI-Transport (Phase 7); sonst sperren |
| Hamlib-X6200 ist jung (Version `.1`) | Eigenheiten bei Split/Modus/Meter | Modell 3087 als Fallback; im Zweifel eigene CI-V-Schicht nur für die ~10 benötigten Befehle (Tabelle liegt in der Firmware-Doku) |
| Host stirbt während CAT-PTT | Sender bleibt getastet | Abschnitt 6, Phase-0-Messung, Doku-Warnung |
| `btcat`-Serialisierung + Transceive | Verzögerte/verschränkte Antworten | Pollrate begrenzen, Transceive prüfen |
| ALC/Prozessor verzerrt Digitalsignale | Schlechte Dekodierung, Splatter | Pegelkalibrierung, Prozessor aus, RADE/FT8 mit Gegenstation prüfen |
| Taktdrift zwischen USB-Soundkarte und pluto-tx-Takt | Aussetzer bei langen Sendungen | `plughw`-Resampling, Langzeittest (600 s) |
| Refactor von `is_audio_only()` bricht andere Geräte | Regression | Baseline-Tests, kleine Commits, Alias behalten |
| Firmware ändert Befehlssatz (v0.3.x entwickelt sich) | CAT-Verhalten ändert sich | Version in der Doku pinnen, Probe-Skript gleicht ID/Version ab |
| 8 W (Akku 5 W) Grenze | Leistungsregler ≠ Watt am Stecker | Prozentstufe, Leistungsmeter anzeigen |
| Sendebetrieb ohne Lizenz/Bandplan | Rechtlich | Bandprüfung, Doku-Hinweis wie im README |

### Entscheidungen, die ich von dir brauche

1. Soll **TCI** (Phase 7) fest in den Umfang oder nur als Rückfallebene?
2. **Default-Leistungsgrenze** (Vorschlag 10 % ≈ 0,8 W) und Maximalsendedauer (Vorschlag 120 s)?
3. Gerätename in der Registry: `x6200` (nur dieses Gerät) oder `xiegu` (X6100 mit Modell 3087 mitgenommen)?
4. Darf Phase 0 an deinem Gerät mit Dummy-Load laufen, und mit welchem Empfänger (Pluto/HackRF/RTL-SDR) als Messgegenstelle?

---

## Quellen

- pluto-tx: <https://github.com/jochenhammes/pluto-tx> (`pluto_tx/devices/*`, `pluto_tx/flowgraph.py`, `pluto_tx/gui.py`, `tests/test_aioc.py`, `tests/fakes.py`, `README.md`)
- OpenX6X00: <https://github.com/Guru-RF/OpenX6X00> (`README.md`, `docs/MANUAL.md` — Abschnitte *TCI server*, *Bluetooth CAT connection*, *Supported CI-V commands*; `src/cat.cpp`, `src/radio.c`, `src/board.h`, `src/hw/x6200/hw_x6200.c`, `btcat/btcat.c`)
- Hamlib Xiegu-Backend: <https://raw.githubusercontent.com/Hamlib/Hamlib/master/rigs/icom/xiegu.c>
- Hamlib Modellnummern: <https://raw.githubusercontent.com/Hamlib/Hamlib/master/include/hamlib/riglist.h>
