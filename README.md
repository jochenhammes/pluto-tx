# pluto-tx

Eigene FM/SSB(USB)-Sendesoftware für den ADALM-PLUTO (Pluto+, Tezuka-Firmware) und HackRF One, gebaut mit GNU Radio. Dazu `pluto_rx`/`pluto_advanced_rx` als Empfänger-Apps.

Sendebetrieb nur mit gültiger Amateurfunklizenz. Verantwortung für Frequenzwahl, Bandplan und Sendeleistung liegt beim Betreiber.

## Installation

```
./install.sh
```

Für Debian/Ubuntu (`apt-get`). Installiert:

- `gnuradio` (zieht `gr-iio` und `python3-pyqt5` mit)
- `python3-libiio` — rohe libiio-Python-Bindings, für `pluto_tx/safety.py` (TX-Sicherheitsschicht) und `pluto_tx/netutil.py` (Verbindungs-Timeout/Scan)
- `libiio-utils` — `iio_info`/`iio_attr` zur manuellen Fehlersuche
- `avahi-daemon` — löst `*.local`-Hostnamen wie `plutoplus.local` auf (wird aktiviert per `systemctl enable --now`)
- `python3-pyqtgraph` — für `pluto_advanced_rx`s Wasserfall
- `soapysdr-module-hackrf`, `hackrf`, `python3-soapysdr` — für HackRF-Unterstützung in `pluto_tx` (TX) und `pluto_advanced_rx` (RX)
- `soapysdr-module-rtlsdr`, `rtl-sdr` — für RTL-SDR-Unterstützung in `pluto_advanced_rx`
- `git`

Prüft danach per echtem Python-Import, ob alles verfügbar ist, und legt `~/.local/bin/pluto-tx`, `pluto-rx`, `pluto-advanced-rx` an. Beliebig oft wiederholbar.

Nicht Teil des Skripts: der Pluto muss per Netzwerk (`ip:...`) oder USB (`usb:...`) erreichbar sein; bei USB ggf. eigene udev-Regeln für Nicht-root-Zugriff (ungetestet).

### Optional: M17

`install.sh` installiert bewusst kein `gnuradio.m17` (kein apt-Paket, braucht C++/CMake-Build, [gr-m17](https://github.com/M17-Project/gr-m17)). Ohne das ist der M17-Moduseintrag ausgegraut, Rest der App läuft normal.

```
./install-m17.sh
```

Baut `gr-m17` (gepinnter Commit) nach `$HOME/.local`, regeneriert den `pluto-tx`-Starter mit passendem `LD_LIBRARY_PATH`.

### FreeDV braucht kein separates Skript

`libcodec2` mit LPCNet/2020/2020B-Support ist bereits transitive Abhängigkeit von `gnuradio`.

### Optional: RADE V1

Ohne separaten Build ist der RADE-Moduseintrag (in `pluto_tx` **und** `pluto_advanced_rx`) ausgegraut, Rest der App läuft normal.

```
./install-rade.sh
```

Baut [`freedv/rade_c`](https://github.com/freedv/rade_c) (gepinnter Commit, inkl. eigenem gepatchtem Opus/FARGAN-Fork) nach `rade_c/` im Projektverzeichnis (kein `make install` — das Projekt hat kein Install-Target) und regeneriert **beide** Starter (`pluto-tx`, `pluto-advanced-rx`) mit passendem `LD_LIBRARY_PATH` (`librade.so`) und `PATH` (`lpcnet_demo`). Merge-sicher: ein bereits von `install-m17.sh` gesetzter `LD_LIBRARY_PATH`-Eintrag bleibt erhalten.

## Struktur

```
install.sh / install-m17.sh / install-rade.sh
pluto_tx/
├── config.py         # geräteunabhängige Konstanten
├── devices/           # TX-Geräte-Abstraktionsschicht (siehe unten)
│   ├── base.py          # TxDevice-ABC, PowerStage
│   ├── pluto.py          # PlutoDevice (gr-iio + safety.py)
│   └── hackrf.py          # HackRFDevice (gr-soapy)
├── safety.py          # PlutoSafety: rohes python3-libiio, unabhängig von GNU Radio
├── flowgraph.py        # PlutoTxFlowgraph: FM/SSB/M17/FreeDV/RADE-Signalkette
├── dynamics.py         # Kompressor/Limiter
├── freedv_ctypes.py / freedv.py   # FreeDV-Anbindung
├── rade_ctypes.py / lpcnet_subprocess.py / rade.py   # RADE-Anbindung (RadeEncoder)
├── gui.py / app.py
└── da2jh-test.wav       # Standard-Testaufnahme
pluto_rx/                # einfacher RX (Frequenz, Gain, Wasserfall)
pluto_advanced_rx/        # RX mit interaktivem SDR++-artigem Wasserfall (eigenständige Kopie von pluto_rx)
├── devices/           # RX-Geräte-Abstraktionsschicht: PlutoDevice/HackRFDevice/RtlSdrDevice
├── rade_ctypes.py / lpcnet_subprocess.py / rade.py   # RADE-Anbindung (RadeDecoder) -- eigene Kopie, kein Import aus pluto_tx
└── ...
pluto_tx_carrier.py        # Carrier-Test-Skript (Fallback/Referenz)
```

`pluto_rx`/`pluto_advanced_rx` sind unabhängig von `pluto_tx` (RX kann nicht senden, braucht keine TX-Sicherheitsschicht), teilen sich aber Bandplan/URI-Konstanten. `pluto_advanced_rx` ist eine eigenständige Kopie von `pluto_rx`, nicht dessen Ersatz — kann frei weiterentwickelt werden, ohne die stabile `pluto_rx`-App zu berühren.

### `pluto_advanced_rx`: interaktiver Wasserfall

Eigenes [pyqtgraph](https://www.pyqtgraph.org/)-Widget (`waterfall_widget.py`) statt GNU Radios `qtgui.waterfall_sink_c`: Live-Spektrum gekoppelt mit dem Wasserfall, Klick-zum-Tunen, Tuning-Marker, Demod-Bandbreiten-Anzeige, Zoom/Pan, einstellbare Floor/Ceiling-Slider für die Farbskala. Die Demodulator-Breite (`Width (Hz)`) ist ein echter, zur Laufzeit änderbarer Filter (FM: Tiefpass vor dem Demod; SSB: Breite des Bandpass-Demodulators selbst), keine reine Anzeige. Ein eigener `gr.sync_block` (`fft_probe.py`) berechnet die FFT und hält sie für einen `QTimer`-Poll bereit, da `qtgui`-Blöcke keinen Datenausgang haben.

RX-Bandbreiten-Presets bis 10 MHz (`1/2,5/5/8/10 MHz`); 15/20 MHz sind bewusst nicht enthalten — bei diesem Dezimationsverhältnis wird der IF-Filter zu lang für den GNU-Radio-Scheduler (siehe ToDo).

## Start

```
pluto-tx
pluto-rx
pluto-advanced-rx
```

Oder direkt: `python3 -m pluto_tx.app --freq 432150000 --gui` (analog für `pluto_rx`/`pluto_advanced_rx`). TX ohne `--gui`: headless CLI-Test.

**M17 direkt gestartet (nicht über den `pluto-tx`-Starter) bleibt ausgegraut** — `LD_LIBRARY_PATH` wird nur vom generierten Starter gesetzt:

```
export LD_LIBRARY_PATH="$HOME/.local/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
python3 -m pluto_tx.app --freq 432150000 --gui
```

## Sicherheitsdesign

Der AD9361-Treiber (libiio) trennt Buffer-Streaming von Dämpfung (`hardwaregain`) und LO-Zustand (`powerdown`) — keine getestete SDR-Software setzt diese beim Stoppen automatisch zurück. `PlutoSafety` (`safety.py`) verwaltet TX-Dämpfung und LO-Powerdown deshalb komplett unabhängig von GNU Radio über rohes `python3-libiio`. `force_safe_state()` (Dämpfung Minimum + LO aus) ist die einzige Funktion, auf die jeder Shutdown-Pfad läuft: normales Programmende, Fenster schließen, SIGINT/SIGTERM, unbehandelte Exceptions. Die GUI zeigt alle 500ms den tatsächlichen Hardware-Zustand an und hat einen NOTAUS-Button.

PTT ist in jedem Modus und auf jedem Gerät hart mit dem tatsächlichen HF-Abschalten verknüpft, nicht nur mit einer Pegelabsenkung: bei Pluto zusätzlich LO-Powerdown (reine Dämpfung unterdrückt LO-Leckage nicht ausreichend), bei HackRF ein Neuaufbau der Geräteverbindung (Nullen der Gain-Register allein stoppt die Sendung nachweislich nicht). `tx_gain` (letzter Block vor dem Gerät) startet stumm und wird nur während einer aktiven Sendung entstummt — wichtig, weil z.B. FM-Modulation von Stille mathematisch ein voller Träger ist, kein Nullsignal.

## Geräte-Abstraktion (`pluto_tx/devices/`)

`TxDevice` (`devices/base.py`) kapselt gerätespezifisches Verhalten: `build_sink()` liefert den GNU-Radio-Block, `power_stages`/`set_power()` das Pegelmodell, `pre_key()`/`post_unkey()` die PTT-Hooks, `force_safe_state()`/`read_hw_state()` Sicherheit und Status. Ein neues Backend braucht nur eine neue Datei + Registry-Eintrag, keine Änderung an `flowgraph.py`.

- **PlutoDevice**: eine Dämpfungsstufe (`MIN_ATTEN`/`MAX_ATTEN` in `config.py`), 0 = volle Leistung.
- **HackRFDevice** (`gnuradio.soapy.sink`): zwei additive Gain-Stufen — `VGA` (0–47dB, kontinuierlich, IF Gain) und `AMP` (+14dB an/aus, RF Gain). Kein Pluto-Äquivalent zum LO-Powerdown verfügbar (die installierten `gnuradio.soapy`-Bindings bieten kein `activate()`/`deactivate()`) — PTT-Release baut deshalb die Geräteverbindung neu auf statt nur den Pegel zu nullen. Manuelle Frequenzkorrektur (`Freq. Correction (Hz)`, GUI, nur bei HackRF sichtbar) gleicht Kristall-Toleranz des jeweiligen Geräts aus — kein allgemeiner HackRF-Wert, pro Gerät empirisch einstellen.

Der TX-Wasserfall zeigt einen auf 50 kHz gezoomten Ausschnitt statt der vollen Geräte-Samplerate (2,5–8+ MHz), damit das eigentliche Signal sichtbar groß ist.

**`pluto_advanced_rx/devices/`** spiegelt dasselbe Prinzip für RX (eigene, einfachere `RxDevice`-ABC — kein PTT, kein Pegel-Ceiling, RX kann nicht senden): **PlutoDevice** (eine AGC-fähige Gain-Stufe), **HackRFDevice** (drei rein manuelle Stufen — LNA 0–40dB, AMP +14dB an/aus, VGA 0–62dB, kein AGC), **RtlSdrDevice** (eine Stufe, echtes Hardware-AGC). Verbindungsfeld akzeptiert bei HackRF/RTL-SDR auch einen vollständigen Soapy-Device-Args-String (`driver=remote,...`) für SoapyRemote-Netzwerkzugriff — dokumentierte Fähigkeit, nicht Ende-zu-Ende getestet (kein Zweitrechner verfügbar).

## NF-Verarbeitung (Noise Gate, Kompressor, Limiter)

```
ptt_mute -> nf_filter (Bandpass) -> gate -> agc -> compressor -> nf_gain -> limiter_smooth -> limiter (Hard-Clip)
```

`gate` (`analog.pwr_squelch_ff`) unterdrückt Rauschen zwischen Wortgruppen vor der AGC. `compressor`/`limiter_smooth` (`pluto_tx/dynamics.py`, eigener Soft-Knee-Kompressor, da GNU Radio keinen fertigen Block dafür hat) machen die Modulation gleichmäßiger bzw. fangen Transienten ab, bevor der bestehende Hard-Clip (`limiter`) greift. `nf_gain` ("Audio Gain") bleibt der einzige Ansteuerungsregler — Kompression macht die Modulation nicht automatisch lauter, nur gleichmäßiger. Alle drei Stufen einzeln aktivierbar, Einstellungen bleiben über Reconnect/Datei-Wechsel erhalten.

## M17 Digitalsprache (TX)

Modus in `pluto_tx` für den offenen [M17](https://m17project.org/)-Standard. Nur TX. Braucht `gnuradio.m17` ([gr-m17](https://github.com/M17-Project/gr-m17)) — ohne das ist der Moduseintrag ausgegraut, Rest der App läuft normal.

```
ptt_mute -> m17_audio_resampler (48k->8k) -> m17_codec2_encoder -> m17_coder (Codec2 -> 4800 Sym/s)
         -> m17_rrc (Root-Raised-Cosine) -> m17_fm_mod (±800Hz Deviation) -> m17_tx_resampler -> tx_gain
```

Zapft `ptt_mute` direkt an (bypasst die NF-Dynamik-Kette — Codec2 braucht keinen davorgeschalteten Sprachprozessor). Geht **nicht** über `mode_selector`: `m17_coder` expandiert das Signal um ~Faktor 520, was GNU Radios Scheduler bei gemeinsamer Führung mit FM/SSB nicht zuverlässig puffern konnte (realer Deadlock, per Stufe-für-Stufe-Sonde auf Hardware bestätigt) — stattdessen eigene, per `lock()/connect()/disconnect()` umgehängte Verbindung zu `tx_gain`.

PTT ist nachrichtenbasiert (SOT/EOT): `unkey_ptt()` sendet EOT, hält die Sendung aber noch `M17_EOT_HOLD_S` (≈0,4s, real vermessen) offen, damit der Encoder sauber abschließt — GUI zeigt das als `"ENDING..."`. NOTAUS umgeht diesen Ablauf und schaltet sofort ab. Rufzeichen (Quelle/Ziel) sind eigene GUI-Felder, wirken direkt ohne Rebuild.

**Verifiziert**: Offline-Loopback-Test, echte Basisband-Messung (korrekte Deviation), realer Sendetest per RTL-SDR bestätigt, unabhängig mit SDR++s M17-Demodulator erfolgreich empfangen.

## FreeDV 2020/2020B Digitalsprache (TX)

Modus in `pluto_tx` für [FreeDV](https://freedv.org/) 2020/2020B (Codec2/LPCNet + OFDM + LDPC-FEC, für HF-SSB-Kanäle). Nur TX. Beide Varianten per GUI-Combo umschaltbar, kein separates Install-Skript nötig.

```
ptt_mute -> freedv_audio_resampler (48k->16k) -> freedv_encoder (freedv_tx(): 16k Sprache -> 8k moduliertes Audio)
         -> freedv_audio_resampler_up (8k->48k) -> freedv_ssb_mod (Hilbert) -> freedv_ssb_resampler -> tx_gain
```

Anders als M17 liefert `freedv_tx()` fertiges moduliertes AUDIO (kein rohes IQ) — genau das, was man ins Mikrofonsignal eines SSB-Transceivers einspeisen würde. Nutzt deshalb dieselbe Hilbert-basierte USB-Modulationstechnik wie normales Sprach-SSB, eigene dedizierte Blockinstanzen. Geht wie M17 nicht über `mode_selector`. PTT ist einfacher als bei M17 (kein SOT/EOT, sofortiges Muten reicht).

**Rufzeichen/Stationskennung ist dauerhaft deaktiviert** — ein bestätigter Bug in der gepackten `libcodec2` (1.2.0-4): das Verknüpfen von FreeDVs `reliable_text`-Textkanal bringt die Bibliothek in einen Zustand, in dem der nächste `freedv_tx()`-Aufruf deterministisch mit SIGFPE abstürzt (per `gdb` bestätigt, unabhängig von Callback/Aufruf von `set_string()`). `FreeDVSession.set_text()` ist ein No-Op, das GUI-Feld dauerhaft deaktiviert (mit Tooltip) — Stationskennung bis auf Weiteres per Sprache vor/nach der Übertragung.

**Verifiziert**: Offline-Blocktest, Konstruktions-/Moduswechsel-Regression, stufenweise Hardware-Sonde ohne Leistungserhöhung, realer Low-Power-Sendetest am RTL-SDR bestätigt.

**Bekannte Einschränkung**: beim realen Sendetest war unterhalb der Trägerfrequenz ein breiteres, gespiegeltes Signalabbild sichtbar (echtes Seitenband-Leck). Die reine digitale Basisbandkette misst dagegen offline sauber (65,5dB USB/LSB-Unterdrückung im belegten Band) — vermutlich AD9361-IQ-Imbalance im analogen Pfad, die eine rein digitale Messung nicht erfasst. OFDM-Signale wie FreeDVs Modem sind dafür empfindlicher als Sprache/M17. Nicht weiter untersucht (siehe ToDo).

## RADE V1 Digitalsprache (TX in `pluto_tx`, RX in `pluto_advanced_rx`)

Modus für [RADE](https://github.com/freedv/rade_c) (Radio Autoencoder, neuronaler HF-Sprachcodec: FARGAN-Vocoder + neuronaler Encoder/Decoder + OFDM). Braucht `librade.so` **und** `lpcnet_demo` (siehe `install-rade.sh`) — ohne die ist der Moduseintrag in beiden Apps ausgegraut. Nur V1 (V2 rät das Projekt selbst von On-Air-Nutzung ab).

Zweistufige Pipeline, kein fertiges GNU-Radio-Bindings vorhanden: `rade_ctypes.py` (ctypes gegen `librade.so`, Sprach-Feature-Vektoren ↔ IQ) + `lpcnet_subprocess.py` (persistenter `lpcnet_demo`-Subprozess für die Sprache↔Feature-Konvertierung, die `librade.so` selbst **nicht** exportiert). Beide Dateien existieren dupliziert in `pluto_tx` und `pluto_advanced_rx` (kein Cross-App-Import, wie überall sonst in diesem Projekt). `lpcnet_demo`s stdout ist von Haus aus voll gepuffert (kein `fflush()` im Quellcode) — der Subprozess läuft deshalb unter `stdbuf -o0 -i0`, sonst blockiert das synchrone Schreiben/Lesen pro Frame.

TX (`pluto_tx/rade.py`, `RadeEncoder`): 16kHz Sprache rein, 8kHz komplexes IQ raus. Wie M17 **nicht** über `mode_selector`, sondern eigene `lock()/connect()/disconnect()`-Verbindung zu `tx_gain`. Ein „End-of-Over"-Tail (144ms, ein Aufruf) existiert, ist aber **standardmäßig aus** (GUI-Checkbox) — braucht noch eine eigene, isolierte Hardware-Verifikation.

RX (`pluto_advanced_rx/rade.py`, `RadeDecoder`): 8kHz IQ rein, 16kHz Sprache raus. Zapft `if_filter`s bereits dezimierten Ausgang an (**nicht** die rohe Gerätequelle direkt — ein Dezimierungsverhältnis von der vollen RX-Bandbreite auf 8kHz sprengt bei Plutos Standard-2,5Msps GNU Radios Puffergrenzen). Kein fester `relative_rate`: die RX-Eingabegröße (`rade_nin()`) variiert mit dem Sync-Zustand. GUI zeigt Sync/Frequenzoffset/SNR, wenn der Modus aktiv ist.

**Verifiziert**: Offline-Rundlauftest (verständliche Sprache, Echtzeitfaktor ≈0,03–0,05), realer Sendetest mit dem Pluto — erfolgreich empfangen und dekodiert sowohl mit SDR++ + externem audio-basiertem RADE-Decoder als auch mit `pluto_advanced_rx` selbst (Sprache gut verständlich).

### Auto Fine-Tune (`pluto_advanced_rx`)

Echter Empfang brauchte manuell viel Frequenz-/Gain-Feintuning (V1 hat anders als V2 **keine** eingebaute Eingangspegel-AGC, siehe `rade_api.h`s `rade_rx_set_agc`-Kommentar — der HF-Frontend-Gain muss stimmen, sonst gibt's keinen Lock). Der "Auto Fine-Tune"-Button im RADE-Modus automatisiert das über zwei Stufen (`rade_autotune.py` + `gui.py`s `_autotune_*`-Zustandsmaschine):

1. **RF-Domäne, schnell** (~1-3s, ohne RADE-Decode): gewichteter Leistungs-Schwerpunkt im FFT-Fenster zentriert die Frequenz, ein SNR-relatives Gain-Anpassen (nicht absolut — siehe unten) bringt den Pegel in einen sinnvollen Bereich.
2. **Gebundene RADE-Lock-Fallback-Suche** (falls Stufe 1 nicht reicht): kleine Frequenz-/Gain-Schritte, jeweils ein paar Sekunden gehalten, bis `rade_decoder.synced` anspringt oder die Suche erschöpft ist (Abbruch per erneutem Klick jederzeit möglich).

**Echter Bug beim Kalibrieren gefunden**: `FftProbe`s dB-Skala ist nicht auf die effektive FFT-Größe normalisiert (unnormierte FFT-Ausgabe) — bei Zoom×16 lag ein Messwert bei +53dB statt eines sinnvollen Pegels. Ein absoluter dB-Zielwert hätte ein bereits übersteuertes RTL-SDR-Frontend fälschlich als "braucht mehr Gain" interpretiert. Fix: das Ziel ist relativ zum lokalen Rauschboden im selben Fenster (SNR, nicht absoluter Pegel) — kürzt den Skalierungsfehler automatisch heraus.

**Real verifiziert** (Pluto→RTL-SDR, 432,15MHz): aus absichtlich verstimmtem Start (+900Hz, Gain 5dB statt optimal ~30dB) fand Auto Fine-Tune über die Stufe-2-Frequenzsuche echten RADE-Sync. `RADE_AUTOTUNE_TARGET_SNR_DB`/`_DWELL_S` (`config.py`) sind aber weiterhin nur grob kalibriert — ein Datenpunkt, eine Antennenaufstellung (siehe ToDo).

## Bekannte Einschränkungen

- **Datei-Wechsel** ("Choose File") baut den Flowgraph komplett neu auf (kein Live-Swap in dieser GNU-Radio-Version) — kurze, aber sichere Unterbrechung.
- Audiodatei loopt unabhängig von PTT weiter, Position wird bei PTT nicht zurückgesetzt.
- **`plutoplus.local` kann nach einem Verbindungswechsel (Ethernet↔USB) kurzzeitig noch auf die alte IP zeigen**, bis `avahi-daemon` nachzieht (paar Sekunden). Zur Kontrolle: `iio_info -u ip:plutoplus.local` oder der GUI-Scan-Button; notfalls IP direkt eintragen.
- **Der AD9361 teilt sich einen Takt zwischen RX und TX**: läuft `pluto_rx` gleichzeitig mit `pluto_tx` und ändert seine RX-Bandbreite, verschiebt das messbar den tatsächlich gesendeten Takt. `pluto_rx` allein ist nicht betroffen. Ungelöst (siehe ToDo) — bis dahin: RX-Bandbreite nicht während aktiver Sendung ändern.
- `pluto_rx`s SSB-Demod (komplexer Bandpass + `complex_to_real`) filtert immer auf eine feste Zwischenfrequenz (`DEMOD_IF_RATE=50kHz`) herunter — ein Bandbreitenwechsel baut daher den Flowgraph neu. 5/10 MHz sind nicht verfügbar: die aktuelle `ip:plutoplus.local`-Verbindung (IIOD-Netzwerkprotokoll) schafft nur ~4,7-4,9 MSa/s, darüber gibt es Overruns/abgehacktes Audio.

## Gerätewahl (Netzwerk/USB)

Editierbares Dropdown ("Device") plus Scan- und Connect/Disconnect-Buttons; Start verbindet automatisch mit `config.DEFAULT_URI` (`ip:plutoplus.local`).

**Nur `pluto_tx`** hat zusätzlich ein "Device Type"-Dropdown (PlutoSDR/HackRF One), das Verbindungsfeld, Frequenzbereich und Pegel-Regler passend umschaltet (nur bei getrenntem Gerät editierbar; HackRF: Seriennummer, leer = einziges angeschlossenes Gerät).

**Scan** ruft `iio.scan_contexts()` auf (mDNS+USB+lokal, ~1s) und füllt das Dropdown. Alternativ manuell: bloßer Hostname/IP (wird zu `ip:...`) oder volle libiio-URI (`usb:1.5.5`, ...).

"Disconnect" fährt sauber herunter (bei `pluto_tx` inkl. `force_safe_state()`); "Connect" baut neu auf und übernimmt alle aktuellen Einstellungen. Schlägt der Aufbau fehl, bleibt die App im getrennten Zustand mit Fehlermeldung statt abzustürzen — der eigentliche Verbindungsversuch hat außerdem ein 5-Sekunden-Timeout (`netutil.py`), da `iio.Context` sonst potenziell sehr lange blockieren kann.

## ToDo für nächstes Mal

- **HackRF-„Spike"-Beobachtung nicht abschließend geklärt**: ein kurzer Ausschlag beim ersten realen PTT-Test konnte mangels präziser Zeitkorrelation nicht sicher als "während der Sendung" (erwartet) oder "im Ruhezustand" (der interessante Fall) eingeordnet werden. Für eine echte Klärung: Aufnahme mit geloggten Zeitstempeln statt Live-Beobachtung.
- **HackRFDevice hat keine von GNU Radio unabhängige Sicherheitsschicht** wie `PlutoSafety` — bewusste Entscheidung, da die aktuellen `gnuradio.soapy`-Bindings dafür keine Grundlage bieten. Falls nötig: roher SoapySDR-Zugriff über `python3-soapysdr`, unabhängig vom laufenden GNU-Radio-Block.
- **RADE-EOO-Tail braucht eigene, isolierte Hardware-Verifikation** — Mechanismus existiert (`rade_eoo_enabled`), ist aber standardmäßig aus und wurde diese Session nicht real getestet (nur der Standardpfad ohne Tail).
- **RADE Auto Fine-Tune (`pluto_advanced_rx`) nur grob kalibriert** — `RADE_AUTOTUNE_TARGET_SNR_DB`/`_DWELL_S`/die Sweep-Schrittweiten (`config.py`) beruhen auf genau einem realen Pluto→RTL-SDR-Testaufbau (432,15MHz, ein Datenpunkt für die SNR-vs-Gain-Kurve). Noch nicht getestet: HackRF als RX-Backend (der Auto-Tune-Code behandelt dessen 3-stufiges Gain-Modell nur über die VGA-Stufe, LNA/AMP bleiben unangetastet), größere absichtliche Verstimmungen, unterschiedliche Antennenaufbauten/Abstände. Der erreichte Lock im realen Test war zudem knapp (~-4dB SNR, kurzzeitig) — bei besserer HF-Verbindung sollte das robuster werden, aber ungetestet.
- **SoapyRemote (Netzwerkzugriff für HackRF/RTL-SDR in `pluto_advanced_rx`) nur als Fähigkeit dokumentiert, nicht Ende-zu-Ende getestet** — kein Zweitrechner verfügbar.
- **FreeDV `reliable_text` beim gepackten libcodec2 (1.2.0-4) melden/reparieren** — echter, per `gdb` bestätigter SIGFPE-Absturz (siehe oben). Nächste Schritte: Bug bei drowe67/codec2 melden, oder neuere libcodec2-Version testen.
- **Vermutete AD9361-IQ-Imbalance untersuchen/kalibrieren** (siehe FreeDV-Abschnitt oben) — braucht einen realen Hardware-Loopback-Test zur Quantifizierung, bevor über eine Korrektur entschieden wird.
- **Geteilter AD9361-Takt zwischen `pluto_rx`/`pluto_tx` beheben** — ein erster Versuch (Takt vor jedem Senden zurückholen) hat das Problem nicht gelöst und wurde wieder entfernt. Nötig: Recherche zu unabhängigem RX/TX-Takt auf dem AD9361/Pluto+.
- **Nativer USB-Backend ungetestet** — könnte den RX-Durchsatz über die aktuellen ~4,7-4,9 MSa/s hinaus verbessern; ggf. eigene udev-Regeln nötig.
- **GUI besser/cooler aussehen lassen** — aktuell rein funktional (Standard-Qt-Widgets).
- **`pluto_advanced_rx`: mehrstufige IF-Dezimation für 15/20-MHz-Presets** — aktuell ein einzelner `rational_resampler_ccf`-Schritt, der bei diesem Verhältnis einen zu langen Filter für den Scheduler erzeugt. Lösung: kaskadierte Dezimation statt einer Stufe.
- **M17-Demodulation in `pluto_advanced_rx`** — TX ist fertig, RX noch offen. `m17.symbol_sync` ist im gr-m17-Quellcode vorhanden aber nicht gebaut; RX müsste `digital.symbol_sync_ff` nutzen. `m17.m17_decoder` braucht einen eigenen `gr.basic_block` als Message-Port-Empfänger für Rufzeichen/Payload.
