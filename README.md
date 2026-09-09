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

Der Pluto muss per Netzwerk (`ip:...`) oder USB (`usb:...`) erreichbar sein — für USB ist nichts weiter nötig (`libiio0`s eigene udev-Regel ist `MODE=666`, weltweit lesbar/schreibbar, verifiziert). Für HackRF/RTL-SDR richtet das Skript zusätzlich zwei Dinge ein, die es nicht ohne Weiteres automatisch abschließen kann:

- Fügt den aktuellen User der Gruppe `plugdev` hinzu (HackRF-/RTL-SDR-udev-Regeln verlangen das, `GROUP=plugdev, MODE=0660`, verifiziert) — **wirkt erst nach Neu-Login**, das Skript kann das selbst nicht auslösen. Bis dahin geht Pluto-Zugriff weiterhin normal.
- Blacklisted den Kernel-DVB-T-Treiber `dvb_usb_rtl28xxu` (`/etc/modprobe.d/blacklist-rtl-sdr.conf`) — RTL-SDR-Sticks werden sonst automatisch vom Kernel als TV-Tuner beansprucht, bevor `librtlsdr`/gr-soapy zugreifen können (der klassische "usb_claim_interface error"-Stolperstein). Ein bereits gestecktes Gerät braucht dafür ein Aus-/Einstecken oder `sudo rmmod dvb_usb_rtl28xxu`, sonst reicht die Blacklist-Datei allein nicht sofort.

### Optional: M17

`install.sh` installiert bewusst kein `gnuradio.m17` (kein apt-Paket, braucht C++/CMake-Build, [gr-m17](https://github.com/M17-Project/gr-m17)). Ohne das ist der M17-Moduseintrag ausgegraut, Rest der App läuft normal.

```
./install-m17.sh
```

Baut `gr-m17` (gepinnter Commit) nach `$HOME/.local`, regeneriert den `pluto-tx`-Starter mit passendem `LD_LIBRARY_PATH`. Installiert dafür zusätzlich `gnuradio-dev` (echter Bug, diese Session gefunden und behoben: `gr-m17` braucht `find_package(Gnuradio REQUIRED)` — das reine Laufzeitpaket `gnuradio` von `install.sh` reicht nicht, `gnuradio-dev` ist nur ein apt-"Recommends", auf schlanken Server-/Container-Installationen oft nicht automatisch dabei).

### FreeDV braucht kein separates Skript

`libcodec2` mit LPCNet/2020/2020B-Support ist bereits transitive Abhängigkeit von `gnuradio`.

### Optional: RADE V1

Ohne separaten Build ist der RADE-Moduseintrag (in `pluto_tx` **und** `pluto_advanced_rx`) ausgegraut, Rest der App läuft normal.

```
./install-rade.sh
```

Baut [`freedv/rade_c`](https://github.com/freedv/rade_c) (gepinnter Commit, inkl. eigenem gepatchtem Opus/FARGAN-Fork) nach `rade_c/` im Projektverzeichnis (kein `make install` — das Projekt hat kein Install-Target) und regeneriert **beide** Starter (`pluto-tx`, `pluto-advanced-rx`) mit passendem `LD_LIBRARY_PATH` (`librade.so`) und `PATH` (`lpcnet_demo`). Merge-sicher: ein bereits von `install-m17.sh` gesetzter `LD_LIBRARY_PATH`-Eintrag bleibt erhalten.

Braucht während des Builds Internetzugriff an **zwei** getrennten Stellen, nicht nur beim offensichtlichen `git clone` (echter Bug, diese Session gefunden und behoben): Opus selbst wird als GitHub-Zip nachgeladen (über CMakes eigenen, eingebauten Downloader, braucht kein extra Tool), aber Opus' `autogen.sh` lädt zusätzlich ein FARGAN/LPCNet-Modell von `media.xiph.org` nach — dafür installiert das Skript jetzt `wget`, das dafür zwingend gebraucht wird und vorher nicht sichergestellt war.

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

**Zweiter echter Bug gefunden und behoben**: das Fenster zur Rauschboden-Messung (±`FINE_TUNE_RANGE_HZ`, 2kHz) überlappte das RADE-Signal selbst (~1,45kHz breit) — der "Rauschboden" war teils vom Signal selbst angehoben, was sowohl die Frequenz-Zentrierung als auch die Gain-SNR-Korrektur verfälschen konnte. Fix: `rade_autotune._noise_floor_excluding()` berechnet den Boden jetzt aus Bins AUSSERHALB eines inneren Bereichs um die Suchmitte (`RADE_AUTOTUNE_NOISE_EXCLUDE_HZ = 1500.0`, `config.py`), nicht mehr aus demselben Fenster wie der Peak. Zusätzlich: der Stufe-2-Frequenz-Sweep ist jetzt dichter/breiter (12 statt 8 Punkte, bis ±1200Hz statt ±800Hz), und ein zuvor stiller No-Op (kein klar erkennbares Signal beim Zentrieren) zeigt jetzt einen Status-Text.

**Real verifiziert** (Pluto→RTL-SDR, 432,15MHz, vor dem Rauschboden-Fix): aus absichtlich verstimmtem Start (+900Hz, Gain 5dB statt optimal ~30dB) fand Auto Fine-Tune über die Stufe-2-Frequenzsuche echten RADE-Sync. `RADE_AUTOTUNE_TARGET_SNR_DB`/`_DWELL_S` (`config.py`) sind aber weiterhin nur grob kalibriert — ein Datenpunkt, eine Antennenaufstellung (siehe ToDo). **Wichtig**: da der Rauschboden jetzt nicht mehr vom Signal selbst angehoben wird, fallen gemessene SNR-Werte tendenziell höher aus als vorher bei gleichem realen Signal — `TARGET_SNR_DB=30.0` sollte nach dem Fix erneut real überprüft werden (siehe ToDo).

### RADE über Soundkarte (externes SSB-Funkgerät)

Alternative zum SDR-Pfad: RADE über ein per Audio-Interface angeschlossenes, klassisches SSB-Funkgerät senden/empfangen, statt über Pluto/HackRF/RTL-SDR. Im `rade_c`-Quellcode verifiziert (nicht angenommen): RADEs komplexes 8kHz-IQ ist so konstruiert, dass **der Realteil allein** direkt als Mono-Audio in ein SSB-Funkgerät passt — die OFDM-Träger sind in `librade.so` bereits fest bei 1500Hz (Mitte des SSB-Passbands) zentriert (`rade_ofdm.c`). Kein Hilbert-Transform, kein Frequenzversatz nötig.

- **TX** (`pluto_tx`): "Soundcard (RADE only)" als gleichwertiger Eintrag neben Pluto/HackRF direkt in der Device-Type-Auswahl (nicht mehr eine RADE-Modus-interne Combo) — wählbare Alternative, nicht gleichzeitig mit einem SDR, kein unnötiges Abstrahlen über den Pluto, wenn nur das externe Funkgerät senden soll. Ist Soundcard gewählt, graut die Mode-Combo alle Modulationsarten außer RADE aus (nur RADEs IQ ist audio-injizierbar). Im Soundkarten-Modus bleibt ein evtl. verbundenes SDR-Gerät bei PTT komplett unberührt (kein LO/Dämpfung-Wechsel) — `complex_to_real` + Resample auf `AUDIO_RATE` (48kHz) → `audio.sink` (Systemstandard).
- **RX** (`pluto_advanced_rx`): "Audio Input" als 4. gleichwertiges Geräte-Backend neben Pluto/HackRF/RTL-SDR, inkl. eigenem Spektrum/Wasserfall (0–10kHz, Aufnahme bei 20kHz). Realer Audio-Eingang wird per `imag=0` + 2×Gain in komplexes IQ gewandelt (exakt `rade_rx_wav.c`s dokumentierte Konvention für reines Audio-Signal, kein Hilbert-Transform) — danach unverändert dieselbe Demod-Kette wie jedes SDR-Backend.

**Verifiziert**: real auf Pluto-Hardware (SDR-Modus unverändert, Audio-Modus lässt das SDR-Gerät bei PTT nachweislich unberührt — Register-Readback identisch vor/während/nach) sowie ein echter Offline-Rundlauf durch die neuen Audio-DSP-Ketten über eine reale 48kHz-Mono-WAV-Datei (RADE-Sync erreicht, SNR 35,7dB). Ein Test mit einem echten externen Funkgerät über ein reales Audio-Interface steht noch aus (siehe ToDo).

## Digimodes (TX in `pluto_tx`)

Erster von mehreren geplanten Digimodes, wählbar in der Mode-Combo, eigener Reiter
"Digimodes". Weitere Digimodes folgen nach und nach in künftigen Sessions.

### Digitext — lesbarer Text im Wasserfall

Sendet einen eingegebenen Text so, dass er beim Empfänger direkt im
Wasserfall/Spektrum als lesbares Bild erscheint — dieselbe Grundidee wie die real
existierende Software ["SonicPhoto"](https://www.kvraudio.com/product/sonicphoto-by-skytopia)
("Reverse Spectrogram Synth") und wie klassisches Hellschreiber im Amateurfunk
(Feld-Hell gilt mit ≈245-367Hz als "narrow band digi mode"). Eigene Implementierung
(`pluto_tx/digitext.py`), reines NumPy + Pillow (Text-Rendering) — direkte additive
Sinuston-Synthese (ein echter, kontinuierlicher Ton pro aktiver Bildspalte, je
Zeile gehalten), wie die gefundenen Referenz-Tools `spectrographic`/`spectrology`.

**Kernmechanik**: Bild-Spalte → Frequenz, Bild-Zeile → Zeit, gesendet Zeile für Zeile
von UNTEN nach oben (nicht oben nach unten) — ein real gefundener Bug: die meisten
Wasserfälle (SDR++, GQRX, die eigene advanced-rx-App) scrollen mit den neuesten
Daten oben, ältere Zeilen rutschen nach unten durch. Zeilen in ihrer natürlichen
Bildreihenfolge (oben zuerst) gesendet erschien der Text deshalb auf dem Kopf; die
Zeilenreihenfolge wird jetzt vor dem Senden gespiegelt (`encode_bitmap_to_audio`),
sodass der Text auf einem normal scrollenden Wasserfall richtig herum steht. Zwei
Layouts, beide mit derselben Grundmechanik, nur unterschiedlich gruppiert:

- **Horizontal**: der ganze Text als eine breite Bitmap. Bandbreite wächst mit der
  Textlänge, live in der GUI angezeigt (kein festes Limit/keine Warnung mehr — per
  Nutzerwunsch abgeschaltet, die Zahl bleibt nur als Orientierung stehen). Dauer
  bleibt fast konstant (~2,0s für ein 5-Zeichen-Wort bei Zoom 1x, `DIGITEXT_ROW_DWELL_S
  = 0.15s`, gesenkt von ursprünglich 0.35s — Buchstaben wirkten sonst auf einem echten
  Wasserfall horizontal gestreckt, siehe ToDo).
- **Vertikal**: jeder Buchstabe einzeln, nacheinander gesendet (gleicher
  Frequenzbereich wiederverwendet, "untereinander"). Bandbreite bleibt konstant
  (≈450Hz, `DIGITEXT_HZ_PER_COL` × Buchstabenbreite) unabhängig von der Textlänge,
  Dauer wächst mit der Zeichenzahl (~1,8s/Zeichen bei Zoom 1x).

**Zoom-Faktor** (GUI, 1x-8x, per Nutzerwunsch): skaliert die gerenderte Bitmap in
beiden Achsen gleichzeitig (nearest-neighbor, `digitext._apply_zoom`) — Buchstaben
werden dadurch größer UND langsamer/breiter gesendet, im gleichen Seitenverhältnis.
Kostet proportional mehr Bandbreite (mehr Spalten) und Sendedauer (mehr Zeilen),
beides weiterhin live in der Schätzung sichtbar.

PTT-Verhalten bewusst anders als bei jedem anderen Modus: einmaliges Senden statt
Halten — PTT-Druck sendet das komplette Bild einmal, die App unkeyt automatisch am
Ende (GUI-Timer, `digitext_duration_s`), kein neuer Sonderfall im Flowgraph nötig
(reines GUI-seitiges Nachlauf-Timing, wiederverwendet dieselbe `_release_ptt()`, die
auch ein manuelles Loslassen/Klicken schon aufruft). Vorzeitiges Loslassen/Klicken
bricht wie gewohnt sofort ab.

Auch über die Soundcard sendbar (externes SSB-Funkgerät, `devices/soundcard.py`) —
neben RADE jetzt der zweite audio-only-taugliche Modus, eigener
`digitext_audio_gain`/`digitext_audio_sink`-Zweig, exakt nach dem RADE-Vorbild
(SDR-Gerät bleibt komplett unberührt, TX-Wasserfall zeigt trotzdem live mit).

**Real verifiziert** (Pluto→RTL-SDR, 432,15MHz, eigenständiges Sende-/Empfangsskript
außerhalb der GUI, echte Aufnahme per RTL-SDR dezimiert und als Spektrogramm
gerendert, visuell gegen den Eingabetext geprüft): sowohl Horizontal ("DA2JH", ganzes
Wort klar lesbar, belegt bei `DIGITEXT_MIN_FREQ_HZ=4000` den Bereich ~4,0-6,16kHz)
als auch Vertikal ("DA2", jeder Buchstabe einzeln klar lesbar, untereinander, ~4,0-
4,9kHz) zeigen den Text tatsächlich korrekt im Wasserfall, mit deutlich sichtbarem
Restrauschen/Körnigkeit im Buchstabenbild (RTL-SDR-Empfangsqualität, siehe ToDo) —
nach insgesamt neun echten Bug-Funden in dieser und der vorherigen Session:

1. **Unlesbarer Text, nur ultraschmalbandiges Signal sichtbar (1. Ursache).** Die
   ursprüngliche ISTFT-Kodierung platzierte den Bildinhalt nur ~23Hz oberhalb von DC
   — mitten in der bekannten Nahbereichs-"Totzone" von `hilbert_fc(401,
   WIN_HAMMING)`. Fix: `DIGITEXT_MIN_FREQ_HZ` weg von DC verschoben (zunächst 300Hz,
   siehe Punkt 5 unten für den finalen Wert).
2. **Unlesbarer Text (2., tiefere Ursache, überlebte Fix 1).** Die ISTFT-Kodierung
   rekonstruiert nur sauber, wenn ein Empfänger zufällig dieselbe interne
   FFT-Größe/Fensterung verwendet wie die Kodierung selbst — was kein echter,
   unabhängiger Empfänger (SDR++, GQRX, ein RTL-SDR-Wasserfall) tut. Offline
   verifiziert: dasselbe Signal sah bei passender Analyse-Auflösung brauchbar aus,
   wurde aber bei unabhängiger/abweichender FFT-Größe zu unlesbarem Rauschen. Fix:
   komplette Umstellung auf direkte additive Sinuston-Synthese (siehe oben) — ein
   echter, kontinuierlicher Ton bei einer Frequenz für eine Dauer ist auf JEDEM
   Spektrum-Display eindeutig sichtbar, unabhängig von dessen FFT-Einstellungen.
3. **Unlesbarer Text (3., subtilste Ursache, im ersten additiven Versuch selbst).**
   Ein Downsampling-Schritt (`DIGITEXT_COL_DOWNSAMPLE=2`, benachbarte Spalten per
   Max-Pooling zusammengefasst, gedacht zur Bandbreiten-Reduktion) verschmolz die
   schmalen Zwischenräume der Buchstaben und machte sie dadurch unkenntlich —
   verifiziert durch einen Vorher-/Nachher-Vergleich rein binärer Schwellenwert-Bilder
   (mit Downsampling: unleserlicher Klecks; ohne: klar "DA2JH"). Fix:
   `DIGITEXT_COL_DOWNSAMPLE = 1` (kein Downsampling mehr) — Bandbreite wird
   stattdessen allein über `DIGITEXT_HZ_PER_COL` und die Horizontal-Längenwarnung
   gesteuert.
4. **Träger lief nach Sendeende unkontrolliert weiter (Vorsichtsmaßnahmen, konnte
   real nicht mehr reproduziert werden nach den obigen Fixes).** `DIGITEXT_TAIL_S`
   (echte Stille ans Wellenform-Ende angehängt) plus ein unbedingter
   Watchdog-Timer (`DIGITEXT_AUTO_UNKEY_WATCHDOG_S`, `_digitext_watchdog_unkey()`,
   nach dem `M17_EOT_HOLD_WATCHDOG_S`-Vorbild) bleiben als Sicherheitsnetz bestehen.
5. **Symmetrisches Spiegelsignal um den Träger, keine lesbaren Buchstaben (5.
   Ursache, nach Fix 1-3 real weiter beobachtet).** Eine erneute reale Aufnahme
   zeigte nur ~13dB Unterdrückung des Spiegelsignals (Ober-/Unterband um den
   Träger) statt der für `hilbert_fc(401, WIN_HAMMING, 6.76)` erwarteten 60-90dB.
   Isolierte Digitalketten-Tests (einzelne Töne, viele gleichzeitige Töne, das
   volle echte Digitext-Audio, mit und ohne Resampler) zeigten dagegen durchweg
   60-94dB Unterdrückung — die Software-Kette ist also nachweislich sauber, und
   die Verschlechterung passiert analog im AD9361-Sendepfad des Pluto selbst
   (IQ-Imbalance), dieselbe bereits für FreeDV/OFDM in diesem Projekt dokumentierte
   Hardware-Eigenschaft. Kein Software-Bug, daher nicht im Code behebbar. Mitigation:
   `DIGITEXT_MIN_FREQ_HZ` von 300Hz auf **4000Hz** angehoben, um das echte Signal und
   sein Spiegelbild auf dem Wasserfall weit genug auseinanderzuschieben, dass sie
   sich nicht mehr optisch überlappen — real erneut getestet (Horizontal "DA2JH",
   Vertikal "DA2"), Spiegelbild in beiden Aufnahmen nicht mehr sichtbar, obwohl das
   zugrundeliegende Unterdrückungsverhältnis unverändert bei ~13-18dB liegt.
6. **FM/SSB nach Digitext-Nutzung aus der GUI nicht mehr erreichbar.** Der
   Mode-Tab-Wechsel (Reiter "Audio"/"Digimodes") war einseitig: das Auswählen von
   Digitext im `mode_combo` (das selbst nur im "Audio"-Reiter sichtbar ist) wechselte
   automatisch auf den "Digimodes"-Reiter, aber ein manueller Klick zurück auf
   "Audio" änderte den aktiven Modus nicht mit -- ohne das versteckte `mode_combo`
   erneut zu finden, gab es keinen offensichtlichen Weg zurück zu FM/SSB. Fix:
   `_on_mode_tab_changed()` macht den Wechsel jetzt bidirektional -- "Audio"
   stellt den zuletzt genutzten Audio-Modus wieder her, "Digimodes" wechselt zu
   Digitext, beide Richtungen real auf Hardware geprüft.
7. **Text erschien auf dem Kopf.** Zeilen wurden in natürlicher Bild-Reihenfolge
   (oben zuerst) gesendet, aber gängige Wasserfälle (SDR++, GQRX, die eigene
   advanced-rx-App) scrollen mit den neuesten Daten oben -- die zuerst gesendete
   (oberste) Bildzeile landet dadurch am Ende ganz unten. Fix: Zeilenreihenfolge
   vor dem Senden gespiegelt (`bitmap[::-1]` in `encode_bitmap_to_audio`) --
   verifiziert mit einem synthetischen Test-Bitmap (unterstes Pixel wird
   nachweislich zuerst gesendet, oberstes zuletzt).
8. **Wiederholtes Senden desselben Texts brach die Übertragung vorzeitig ab.**
   Ursache: der Auto-Unkey-Watchdog identifizierte eine Übertragung nur über die
   (über die ganze Sitzung hinweg gleichbleibende) Flowgraph-Instanz, nicht über
   den einzelnen PTT-Druck. Bei identischem Text (identischer `digitext_duration_s`)
   landete der Watchdog-Timer des VORHERIGEN Drucks reproduzierbar mitten in der
   NÄCHSTEN Übertragung und beendete sie vorzeitig -- sichtbar am Screenshot mit
   mehreren übereinander gestapelten, teils abgeschnittenen Sendungen. Fix: ein
   pro Tastendruck hochgezählter Epochen-Zähler, den beide Timer (Haupttimer und
   Watchdog) gegenprüfen -- ein Timer aus einem älteren Druck ist jetzt zuverlässig
   als veraltet erkennbar und greift nicht mehr in eine neuere, noch laufende
   Übertragung ein. Verifiziert durch gezielte Nachbildung der exakten
   Race-Condition (nicht auf echter Hardware -- reine GUI-Timer-Logik, unabhängig
   von HF/Empfang).
9. **Harte Bandbreiten-Obergrenze bei ~18000Hz, Zeichen langsam ausgefadet statt
   abrupt abgeschnitten.** Nicht der Hilbert-Modulator (`digitext_ssb_mod`,
   offline mit Einzeltönen gemessen: flach bis auf 0.1dB bis 23.8kHz) --
   sondern `digitext_ssb_resampler`s automatisch entworfener Anti-Alias-Filter
   (`fractional_bw=0.4`, wie jeder andere SSB-Resampler in dieser App). Offline
   mit Einzeltönen durch die exakte Resampler-Konfiguration gemessen: bei 0.4
   flach nur bis ~18-19kHz, bei 20kHz schon -1.2dB, bei 23kHz praktisch weg --
   genau das langsame, nicht-abrupte Ausfaden, das gemeldet wurde. Fix: nur für
   `digitext_ssb_resampler` (nicht die anderen SSB-Pfade, die nie in die Nähe
   von 18kHz kommen) `fractional_bw` auf 0.47 angehoben -- verschiebt die
   Passband-Kante Richtung Nyquist (0.5 wäre die mathematische Grenze, bei der
   die nötige Filter-Übergangsbreite gegen null geht). Ergebnis (Ton durch die
   volle Kette Hilbert+Resampler gemessen): flach bis 22kHz, erst danach
   Abfall -- rund 3.5-4kHz mehr nutzbare Bandbreite oberhalb von
   `DIGITEXT_MIN_FREQ_HZ`.

**`errno 113` beim Pluto-Connect geklärt** (kein Code-Bug): `plutoplus.local` löst
per mDNS auf eine Heimnetz-Adresse auf, die vom Rechner aus zeitweise nicht
erreichbar ist, während der Pluto parallel über eine direkte USB-Ethernet-Verbindung
(eine andere IP) einwandfrei erreichbar bleibt — `ping plutoplus.local` schlägt fehl
("Destination Host Unreachable"), `ping` auf die direkte USB-Gadget-IP funktioniert
sofort. Workaround: bei diesem Fehler die direkte IP/USB-Verbindung statt des
Hostnamens verwenden.

**Weiterhin nur ein erster Datenpunkt** (ein Wort, zwei Layouts, eine
Antennenaufstellung) — Bandbreiten-/Timing-Konstanten könnten mit mehr realen Tests
noch feinjustiert werden (leichtes "Streifen"-Rauschen innerhalb der Buchstaben durch
Schwebung zwischen vielen gleichzeitigen Tönen ist sichtbar, stört die Lesbarkeit
aber nicht).

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

- **Digitext (`pluto_tx`) real verifiziert, aber nur ein Datenpunkt** — Pluto→RTL-SDR, 432,15MHz, echte Aufnahme als Spektrogramm gerendert: "DA2JH" (Horizontal) und "DA2" (Vertikal) beide klar lesbar. Fünf echte Bugs unterwegs gefunden und behoben (siehe README-Abschnitt oben), u.a. eine komplette Umstellung von ISTFT- auf additive Sinuston-Kodierung sowie das Verschieben von `DIGITEXT_MIN_FREQ_HZ` auf 4000Hz gegen das analoge AD9361-Spiegelsignal. Noch offen: mehr Testwörter, andere Antennenaufstellungen/Abstände, ob die AD9361-IQ-Imbalance selbst (~13-18dB Unterdrückung) durch eine Kalibrierung statt nur durch Frequenzverschiebung reduzierbar wäre, ob das sichtbare Rauschen/Körnigkeit innerhalb der Buchstaben (vermutlich RTL-SDR-Empfangsqualität) mit weiteren Parameteranpassungen reduzierbar ist. Weitere Digimodes sind für künftige Sessions geplant.
- **`plutoplus.local` teils nicht erreichbar (`errno 113`)** — mDNS löst auf eine Heimnetz-Adresse auf, die nicht immer erreichbar ist, während die direkte USB-Ethernet-IP des Pluto zuverlässig funktioniert (siehe Digitext-Abschnitt oben für die Diagnose). Kein Code-Bug in `pluto_tx`; ggf. Netzwerk-/Routing-Konfiguration prüfen oder die direkte IP statt des Hostnamens verwenden.
- **HackRF-„Spike"-Beobachtung nicht abschließend geklärt**: ein kurzer Ausschlag beim ersten realen PTT-Test konnte mangels präziser Zeitkorrelation nicht sicher als "während der Sendung" (erwartet) oder "im Ruhezustand" (der interessante Fall) eingeordnet werden. Für eine echte Klärung: Aufnahme mit geloggten Zeitstempeln statt Live-Beobachtung.
- **HackRFDevice hat keine von GNU Radio unabhängige Sicherheitsschicht** wie `PlutoSafety` — bewusste Entscheidung, da die aktuellen `gnuradio.soapy`-Bindings dafür keine Grundlage bieten. Falls nötig: roher SoapySDR-Zugriff über `python3-soapysdr`, unabhängig vom laufenden GNU-Radio-Block.
- **RADE-EOO-Tail braucht eigene, isolierte Hardware-Verifikation** — Mechanismus existiert (`rade_eoo_enabled`), ist aber standardmäßig aus und wurde diese Session nicht real getestet (nur der Standardpfad ohne Tail).
- **RADE Auto Fine-Tune (`pluto_advanced_rx`) nur grob kalibriert** — `RADE_AUTOTUNE_TARGET_SNR_DB`/`_DWELL_S`/die Sweep-Schrittweiten (`config.py`) beruhen auf genau einem realen Pluto→RTL-SDR-Testaufbau (432,15MHz, ein Datenpunkt für die SNR-vs-Gain-Kurve). Noch nicht getestet: HackRF als RX-Backend (der Auto-Tune-Code behandelt dessen 3-stufiges Gain-Modell nur über die VGA-Stufe, LNA/AMP bleiben unangetastet), größere absichtliche Verstimmungen, unterschiedliche Antennenaufbauten/Abstände. Der erreichte Lock im realen Test war zudem knapp (~-4dB SNR, kurzzeitig) — bei besserer HF-Verbindung sollte das robuster werden, aber ungetestet. **Neu diese Session**: der Rauschboden-Fenster-Überlapp-Bug ist behoben (`_noise_floor_excluding()`), dadurch dürften gemessene SNR-Werte jetzt tendenziell höher ausfallen als vorher — `TARGET_SNR_DB=30.0` braucht deswegen einen frischen realen Kalibrierlauf, noch nicht gemacht. Sequentielle Freq→Gain-Architektur bewusst nicht angetastet (siehe README-Abschnitt oben).
- **SoapyRemote (Netzwerkzugriff für HackRF/RTL-SDR in `pluto_advanced_rx`) nur als Fähigkeit dokumentiert, nicht Ende-zu-Ende getestet** — kein Zweitrechner verfügbar.
- **RADE über Soundkarte nicht mit echtem externem SSB-Funkgerät getestet** — TX/RX-Pfad ist real auf Pluto-Hardware sicherheitsverifiziert und der Audio-DSP-Rundlauf offline über eine echte WAV-Datei bestätigt (Sync, SNR 35,7dB), aber ein Test mit echtem Audio-Interface + Transceiver steht noch aus (kein solches Gerät verfügbar).
- **FreeDV `reliable_text` beim gepackten libcodec2 (1.2.0-4) melden/reparieren** — echter, per `gdb` bestätigter SIGFPE-Absturz (siehe oben). Nächste Schritte: Bug bei drowe67/codec2 melden, oder neuere libcodec2-Version testen.
- **Vermutete AD9361-IQ-Imbalance untersuchen/kalibrieren** (siehe FreeDV-Abschnitt oben) — braucht einen realen Hardware-Loopback-Test zur Quantifizierung, bevor über eine Korrektur entschieden wird.
- **Geteilter AD9361-Takt zwischen `pluto_rx`/`pluto_tx` beheben** — ein erster Versuch (Takt vor jedem Senden zurückholen) hat das Problem nicht gelöst und wurde wieder entfernt. Nötig: Recherche zu unabhängigem RX/TX-Takt auf dem AD9361/Pluto+.
- **Nativer USB-Backend ungetestet** — könnte den RX-Durchsatz über die aktuellen ~4,7-4,9 MSa/s hinaus verbessern. Die udev-Berechtigungsfrage ist inzwischen geklärt (diese Session verifiziert): `libiio0`s eigene Regel ist `MODE=666`, kein Gruppenzwang nötig — der Vorbehalt hier betraf nur die Durchsatzmessung selbst, nicht mehr die Zugriffsrechte.
- **GUI besser/cooler aussehen lassen** — aktuell rein funktional (Standard-Qt-Widgets).
- **`pluto_advanced_rx`: mehrstufige IF-Dezimation für 15/20-MHz-Presets** — aktuell ein einzelner `rational_resampler_ccf`-Schritt, der bei diesem Verhältnis einen zu langen Filter für den Scheduler erzeugt. Lösung: kaskadierte Dezimation statt einer Stufe.
- **M17-Demodulation in `pluto_advanced_rx`** — TX ist fertig, RX noch offen. `m17.symbol_sync` ist im gr-m17-Quellcode vorhanden aber nicht gebaut; RX müsste `digital.symbol_sync_ff` nutzen. `m17.m17_decoder` braucht einen eigenen `gr.basic_block` als Message-Port-Empfänger für Rufzeichen/Payload.
