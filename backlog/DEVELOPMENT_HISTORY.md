# pluto-tx: Entwicklungshistorie und Implementierungsdetails

Dies ist die rohe Entwicklungsgeschichte hinter `pluto-tx`/`pluto_advanced_rx`/
`pluto_cli` — gefundene Bugs, Verifikationsergebnisse, interne Architektur-
Details. Ausgelagert aus dem Haupt-`README.md`, das jetzt als reines
Showcase/Manual für Nutzer gedacht ist; hier steht das "Wie/Warum wurde es
so gebaut, was ist unterwegs schiefgelaufen". Gegenstück zu
`backlog/ft8/FT8_HANDOFF.md`.

**Wie benutzen**: wenn ein Bug in einem der hier beschriebenen Bereiche
wieder auftaucht, hier zuerst nachlesen, bevor von vorne debuggt wird —
viele der unten beschriebenen Fixes hatten nicht-offensichtliche Ursachen.

---

## Geräte-Abstraktion (`pluto_tx/devices/`, `pluto_advanced_rx/devices/`)

`TxDevice` (`devices/base.py`) kapselt gerätespezifisches Verhalten:
`build_sink()` liefert den GNU-Radio-Block, `power_stages`/`set_power()`
das Pegelmodell, `pre_key()`/`post_unkey()` die PTT-Hooks,
`force_safe_state()`/`read_hw_state()` Sicherheit und Status. Ein neues
Backend braucht nur eine neue Datei + Registry-Eintrag, keine Änderung an
`flowgraph.py`.

- **PlutoDevice**: eine Dämpfungsstufe (`MIN_ATTEN`/`MAX_ATTEN` in
  `config.py`), 0 = volle Leistung.
- **HackRFDevice** (`gnuradio.soapy.sink`): zwei additive Gain-Stufen —
  `VGA` (0–47dB, kontinuierlich, IF Gain) und `AMP` (+14dB an/aus, RF
  Gain). Kein Pluto-Äquivalent zum LO-Powerdown verfügbar (die
  installierten `gnuradio.soapy`-Bindings bieten kein
  `activate()`/`deactivate()`) — PTT-Release baut deshalb die
  Geräteverbindung neu auf statt nur den Pegel zu nullen. Manuelle
  Frequenzkorrektur (`Freq. Correction (Hz)`, GUI, nur bei HackRF
  sichtbar) gleicht Kristall-Toleranz des jeweiligen Geräts aus — kein
  allgemeiner HackRF-Wert, pro Gerät empirisch einstellen.

Der TX-Wasserfall zeigt einen auf 50 kHz gezoomten Ausschnitt statt der
vollen Geräte-Samplerate (2,5–8+ MHz), damit das eigentliche Signal
sichtbar groß ist.

`pluto_advanced_rx/devices/` spiegelt dasselbe Prinzip für RX (eigene,
einfachere `RxDevice`-ABC — kein PTT, kein Pegel-Ceiling, RX kann nicht
senden): **PlutoDevice** (eine AGC-fähige Gain-Stufe), **HackRFDevice**
(drei rein manuelle Stufen — LNA 0–40dB, AMP +14dB an/aus, VGA 0–62dB,
kein AGC), **RtlSdrDevice** (eine Stufe, echtes Hardware-AGC).
Verbindungsfeld akzeptiert bei HackRF/RTL-SDR auch einen vollständigen
Soapy-Device-Args-String (`driver=remote,...`) für SoapyRemote-
Netzwerkzugriff — dokumentierte Fähigkeit, nicht Ende-zu-Ende getestet
(kein Zweitrechner verfügbar).

Zwei kritische, sicherheitsrelevante Bugs wurden bei echten
HackRF-Hardware-Tests gefunden und behoben:

1. **HackRF sendete nach PTT-Freigabe weiter; NOTAUS half nicht, nur
   Disconnect.** Root Cause: HackRFs Software-Gain-Regler (VGA=0dB,
   AMP aus) unterdrücken die HF-Ausgabe NICHT tatsächlich wie Plutos
   ~90dB-Dämpfer — empirisch am echten Gerät bestätigt, und im Code
   bestätigt, dass `gnuradio.soapy`s installierte Python-Bindings kein
   sicheres `activate()`/`deactivate()` mitten im Lauf anbieten. Fix:
   `TxDevice.supports_persistent_sink`-Flag (`False` für HackRF); wenn
   `False`, schließen/öffnen `unkey_ptt()`/`finish_unkey_m17()`/NOTAUS
   den Sink-Block komplett neu, statt nur den Pegel zu nullen.
2. **Tieferer Bug, direkt nach Fix #1 gefunden: ein Träger war ab
   App-Start in FM-Modus präsent, vor jedem PTT-Druck.** Root Cause:
   `tx_gain` (letzter Block vor dem Gerät) war IMMER ein permanenter
   Durchlass (`×1`, nie stumm) — nur `ptt_mute` (vor der Modulation)
   folgte dem PTT-Status. Für SSB/M17 unproblematisch (Stille rein ->
   Stille/protokollgesteuert raus), aber FM-Modulation von Stille ist
   mathematisch ein voller unmodulierter Träger. War schon vor HackRF
   immer wahr, nur bei Pluto durch dessen ~90dB-Dämpfer + LO-Powerdown
   unsichtbar. Fix: `tx_gain` startet jetzt stumm (`0.0+0j`) und wird
   explizit im Lockstep mit `ptt_mute` entstummt/gemutet.

---

## M17 Digitalsprache (TX in `pluto_tx`, RX in `pluto_advanced_rx`)

```
ptt_mute -> m17_audio_resampler (48k->8k) -> m17_codec2_encoder -> m17_coder (Codec2 -> 4800 Sym/s)
         -> m17_rrc (Root-Raised-Cosine) -> m17_fm_mod (±800Hz Deviation) -> m17_tx_resampler -> tx_gain
```

Zapft `ptt_mute` direkt an (bypasst die NF-Dynamik-Kette — Codec2 braucht
keinen davorgeschalteten Sprachprozessor). Geht **nicht** über
`mode_selector`: `m17_coder` expandiert das Signal um ~Faktor 520, was
GNU Radios Scheduler bei gemeinsamer Führung mit FM/SSB nicht
zuverlässig puffern konnte (realer Deadlock, per Stufe-für-Stufe-Sonde
auf Hardware bestätigt) — stattdessen eigene, per
`lock()/connect()/disconnect()` umgehängte Verbindung zu `tx_gain`.

**Erster "funktioniert"-Test war unvollständig.** Frühe Tests (Offline-
Coder->Decoder-Loopback, echte PTT-Timing-Tests) prüften nur den
GNU-Radio-Python-Zustand (Dämpfungsregister, `keyed`-Flag, Anzeige-Text)
— nie den tatsächlich gesendeten HF-Inhalt. Echte RTL-SDR-Überwachung
fand: M17 sendete nur einen bloßen unmodulierten Träger, kein echtes
M17-Signal. Root Cause, per Stufe-für-Stufe-Vector-Sink-Sonde direkt am
echten laufenden Flowgraph gefunden: `m17_coder` erzwingt
`output_multiple(192)` und expandiert stromabwärts ~520x; über
`blocks.selector` (als 3. Eingang neben FM/SSB) konnte GNU Radios
Scheduler nie Puffer verhandeln — `general_work()` wurde nach PTT/SOT
nie wieder aufgerufen. `set_max_output_buffer()` behebt es nicht (GNU
Radio deckelt es intern viel niedriger als nötig). Fix wie oben: M17
umgeht `mode_selector` komplett.

PTT ist nachrichtenbasiert (SOT/EOT): `unkey_ptt()` sendet EOT, hält die
Sendung aber noch `M17_EOT_HOLD_S` (≈0,4s, real vermessen) offen, damit
der Encoder sauber abschließt — GUI zeigt das als `"ENDING..."`. NOTAUS
umgeht diesen Ablauf und schaltet sofort ab.

**Verifiziert**: Offline-Loopback-Test, echte Basisband-Messung (korrekte
Deviation, ~1.8kHz RMS/~3.5kHz Peak), realer Sendetest per RTL-SDR
bestätigt, unabhängig mit SDR++s M17-Demodulator erfolgreich empfangen —
echter Beweis eines standardkonformen Signals, nicht nur "sieht moduliert
aus".

**M17-RX** (`pluto_advanced_rx`): `gr-m17` hatte bereits einen
funktionierenden `m17.m17_decoder`/`m17.codec2_decoder` (vom TX-Teil
vendort), nur nie in RX verdrahtet. Brauchte nur GNU Radios Standard
`digital.symbol_sync_ff` (gr-m17s eigener `m17.symbol_sync` existiert im
Quellcode, ist in diesem Checkout aber nicht einkompiliert). Verifiziert
mit echtem Pluto-TX -> RTL-SDR-RX-Over-the-Air-Test bei 432,15MHz/-50dB:
15/15 Frames korrekt dekodiert inkl. Rufzeichen.

**M17-TX-"Chopping"-Bug (2026-09-16, gefunden und behoben):** periodische
Aussetzer im übertragenen Signal bei längeren Sendungen. Root Cause:
`blocks.throttle`s unbegrenzte Standard-Chunk-Größe ließ große
Audio-Bursts (Datei/synthetische Quelle) auf einmal durch; `m17_coder`
UND `codec2_encoder` (beide `set_output_multiple(...)`-Blöcke ohne
Input-Lookahead) konnten das nicht glatt absorbieren — anders als ein
echter ALSA-Mikrofon-Callback mit kleinen, regelmäßigen, hardware-
getakteten Chunks. Fix: `file_throttle`s `maximum_items_per_chunk`
begrenzt auf `config.FILE_THROTTLE_CHUNK_SAMPLES = 960` (48kHz-Samples =
ein volles Codec2-3200bps-Frame, 160 Samples @ 8kHz, nach dem 6:1
`m17_audio_resampler`). Vom User auf echter Hardware bestätigt.

Nebenbug, gleiche Untersuchung: File Broadcasts Hintergrund-TX-Zweig
hatte keine Ratenbegrenzung und verbrauchte durchgehend ~96-99% eines
CPU-Kerns, unabhängig vom aktiven Modus — mit einem separaten
`blocks.throttle` behoben.

---

## FreeDV 2020/2020B Digitalsprache (TX in `pluto_tx`)

```
ptt_mute -> freedv_audio_resampler (48k->16k) -> freedv_encoder (freedv_tx(): 16k Sprache -> 8k moduliertes Audio)
         -> freedv_audio_resampler_up (8k->48k) -> freedv_ssb_mod (Hilbert) -> freedv_ssb_resampler -> tx_gain
```

Anders als M17 liefert `freedv_tx()` fertiges moduliertes AUDIO (kein
rohes IQ) — genau das, was man ins Mikrofonsignal eines
SSB-Transceivers einspeisen würde. Nutzt deshalb dieselbe Hilbert-
basierte USB-Modulationstechnik wie normales Sprach-SSB. Geht wie M17
nicht über `mode_selector`.

**Kritischer Absturz-Bug (App crashte bei jedem Start).** Root Cause,
per `gdb`-Backtrace bestätigt: `reliable_text_use_with_freedv()` (FreeDVs
Stationskennungs-Textkanal) korrumpiert bei der gepackten `libcodec2`
(1.2.0-4) den FreeDV-2020/2020B-Sitzungszustand — der nächste
`freedv_tx()`-Aufruf stürzt danach deterministisch mit SIGFPE
(Ganzzahl-Division durch Null in `freedv_comptx_2020()`) ab. Reproduziert
unabhängig von der eigenen ctypes-Wrapper-Klasse, auch mit echtem
Callback, auch ohne `set_string()`-Aufruf — Linken allein reicht. Da der
FreeDV-Zweig immer konstruiert wird und ab App-Start kontinuierlich Audio
verarbeitet, crashte die App fast sofort, unabhängig vom gewählten Modus.
Fix: `reliable_text`-Anbindung komplett aus `FreeDVSession` entfernt
(`freedv_ctypes.py`); `set_text()` ist jetzt ein No-Op, das GUI-Feld
dauerhaft deaktiviert (mit Tooltip) statt es funktionslos live wirken zu
lassen.

**Verifiziert**: Offline-Blocktest, Konstruktions-/Moduswechsel-
Regression, stufenweise Hardware-Sonde ohne Leistungserhöhung, realer
Low-Power-Sendetest am RTL-SDR bestätigt.

**Bekannte Einschränkung**: beim realen Sendetest war unterhalb der
Trägerfrequenz ein breiteres, gespiegeltes Signalabbild sichtbar (echtes
Seitenband-Leck). Die reine digitale Basisbandkette misst dagegen
offline sauber (65,5dB USB/LSB-Unterdrückung im belegten Band) —
vermutlich AD9361-IQ-Imbalance im analogen Pfad, die eine rein digitale
Messung nicht erfasst. OFDM-Signale wie FreeDVs Modem sind dafür
empfindlicher als Sprache/M17. Nicht weiter untersucht.

---

## RADE V1 Digitalsprache (TX in `pluto_tx`, RX in `pluto_advanced_rx`)

Zweistufige Pipeline, kein fertiges GNU-Radio-Binding vorhanden:
`rade_ctypes.py` (ctypes gegen `librade.so`, Sprach-Feature-Vektoren ↔
IQ) + `lpcnet_subprocess.py` (persistenter `lpcnet_demo`-Subprozess für
die Sprache↔Feature-Konvertierung, die `librade.so` selbst **nicht**
exportiert). Beide Dateien existieren dupliziert in `pluto_tx` und
`pluto_advanced_rx` (kein Cross-App-Import, wie überall sonst in diesem
Projekt). `lpcnet_demo`s stdout ist von Haus aus voll gepuffert (kein
`fflush()` im Quellcode) — der Subprozess läuft deshalb unter
`stdbuf -o0 -i0`.

TX (`pluto_tx/rade.py`, `RadeEncoder`): 16kHz Sprache rein, 8kHz
komplexes IQ raus. Wie M17 eigene `lock()/connect()/disconnect()`-
Verbindung zu `tx_gain`. Ein „End-of-Over"-Tail (144ms) existiert, ist
standardmäßig aus (GUI-Checkbox) — bräuchte noch eine eigene, isolierte
Hardware-Verifikation.

RX (`pluto_advanced_rx/rade.py`, `RadeDecoder`): 8kHz IQ rein, 16kHz
Sprache raus. Zapft `if_filter`s bereits dezimierten Ausgang an (nicht
die rohe Gerätequelle direkt — ein Dezimierungsverhältnis von der vollen
RX-Bandbreite auf 8kHz sprengt bei Plutos Standard-2,5Msps GNU Radios
Puffergrenzen). Kein fester `relative_rate`: die RX-Eingabegröße
(`rade_nin()`) variiert mit dem Sync-Zustand.

**Verifiziert**: Offline-Rundlauftest (verständliche Sprache,
Echtzeitfaktor ≈0,03–0,05), realer Sendetest mit dem Pluto — erfolgreich
empfangen und dekodiert sowohl mit SDR++ + externem audio-basiertem
RADE-Decoder als auch mit `pluto_advanced_rx` selbst.

### Auto Fine-Tune (`pluto_advanced_rx`)

RADE V1 hat anders als V2 **keine** eingebaute Eingangspegel-AGC (siehe
`rade_api.h`s `rade_rx_set_agc`-Kommentar) — der HF-Frontend-Gain muss
stimmen, sonst gibt's keinen Lock. Der "Auto Fine-Tune"-Button
automatisiert das zweistufig (`rade_autotune.py` + `gui.py`s
`_autotune_*`-Zustandsmaschine):

1. **RF-Domäne, schnell** (~1-3s, ohne RADE-Decode): gewichteter
   Leistungs-Schwerpunkt im FFT-Fenster zentriert die Frequenz, ein
   SNR-relatives Gain-Anpassen bringt den Pegel in einen sinnvollen
   Bereich.
2. **Gebundene RADE-Lock-Fallback-Suche**: kleine Frequenz-/Gain-
   Schritte, jeweils ein paar Sekunden gehalten, bis
   `rade_decoder.synced` anspringt oder die Suche erschöpft ist.

**Bug 1 beim Kalibrieren gefunden**: `FftProbe`s dB-Skala ist nicht auf
die effektive FFT-Größe normalisiert — bei Zoom×16 lag ein Messwert bei
+53dB statt eines sinnvollen Pegels. Ein absoluter dB-Zielwert hätte ein
bereits übersteuertes Frontend fälschlich als "braucht mehr Gain"
interpretiert. Fix: das Ziel ist relativ zum lokalen Rauschboden im
selben Fenster (SNR, nicht absoluter Pegel).

**Bug 2 gefunden**: das Fenster zur Rauschboden-Messung (±2kHz)
überlappte das RADE-Signal selbst (~1,45kHz breit) — der "Rauschboden"
war teils vom Signal selbst angehoben. Fix:
`rade_autotune._noise_floor_excluding()` berechnet den Boden aus Bins
AUSSERHALB eines inneren Bereichs um die Suchmitte
(`RADE_AUTOTUNE_NOISE_EXCLUDE_HZ = 1500.0`).

**Real verifiziert** (Pluto→RTL-SDR, 432,15MHz): aus absichtlich
verstimmtem Start (+900Hz, Gain 5dB statt optimal ~30dB) fand Auto
Fine-Tune über die Stufe-2-Frequenzsuche echten RADE-Sync. Nur ein
Datenpunkt, eine Antennenaufstellung — `RADE_AUTOTUNE_TARGET_SNR_DB`/
`_DWELL_S` (`config.py`) sind weiterhin nur grob kalibriert.

### RADE über Soundkarte (externes SSB-Funkgerät)

Im `rade_c`-Quellcode verifiziert: RADEs komplexes 8kHz-IQ ist so
konstruiert, dass der Realteil allein direkt als Mono-Audio in ein
SSB-Funkgerät passt — die OFDM-Träger sind in `librade.so` bereits fest
bei 1500Hz (Mitte des SSB-Passbands) zentriert (`rade_ofdm.c`). Kein
Hilbert-Transform, kein Frequenzversatz nötig.

- **TX**: "Soundcard (RADE only)" als gleichwertiger Eintrag neben
  Pluto/HackRF direkt in der Device-Type-Auswahl. Im Soundkarten-Modus
  bleibt ein evtl. verbundenes SDR-Gerät bei PTT komplett unberührt.
- **RX**: "Audio Input" als 4. gleichwertiges Geräte-Backend. Realer
  Audio-Eingang wird per `imag=0` + 2×Gain in komplexes IQ gewandelt
  (exakt `rade_rx_wav.c`s dokumentierte Konvention).

**Verifiziert**: real auf Pluto-Hardware (SDR-Modus unverändert,
Audio-Modus lässt das SDR-Gerät bei PTT nachweislich unberührt) sowie
ein echter Offline-Rundlauf über eine reale WAV-Datei (RADE-Sync
erreicht, SNR 35,7dB). Test mit echtem externen Funkgerät steht noch
aus.

---

## Digimodes: Waterfall Writer (ehem. "Digitext", TX in `pluto_tx`)

Sendet einen eingegebenen Text so, dass er beim Empfänger direkt im
Wasserfall/Spektrum als lesbares Bild erscheint — dieselbe Grundidee wie
["SonicPhoto"](https://www.kvraudio.com/product/sonicphoto-by-skytopia)
("Reverse Spectrogram Synth") und klassisches Hellschreiber im
Amateurfunk. Eigene Implementierung (`pluto_tx/digitext.py`), reines
NumPy + Pillow — direkte additive Sinuston-Synthese (ein echter,
kontinuierlicher Ton pro aktiver Bildspalte, je Zeile gehalten).

**Neun echte Bugs gefunden und behoben** (Pluto→RTL-SDR-Realtests,
Spektrogramm-Vergleich gegen den Eingabetext):

1. **Unlesbarer Text (1. Ursache).** Ursprüngliche ISTFT-Kodierung
   platzierte den Bildinhalt nur ~23Hz oberhalb DC — in der Totzone von
   `hilbert_fc(401, WIN_HAMMING)`. Fix: Signal weg von DC verschoben.
2. **Unlesbarer Text (2., tiefere Ursache).** ISTFT-Kodierung
   rekonstruiert nur sauber, wenn der Empfänger zufällig dieselbe
   interne FFT-Größe/Fensterung nutzt wie die Kodierung — kein echter
   unabhängiger Empfänger tut das. Fix: komplette Umstellung auf direkte
   additive Sinuston-Synthese.
3. **Unlesbarer Text (3., subtilste Ursache).** Ein
   Downsampling-Schritt (`DIGITEXT_COL_DOWNSAMPLE=2`, Max-Pooling)
   verschmolz schmale Zwischenräume der Buchstaben. Fix:
   `DIGITEXT_COL_DOWNSAMPLE = 1`.
4. **Träger lief nach Sendeende potenziell weiter.** Vorsichtsmaßnahme:
   `DIGITEXT_TAIL_S` (echte Stille angehängt) plus unbedingter
   Watchdog-Timer als Sicherheitsnetz.
5. **Symmetrisches Spiegelsignal um den Träger.** Reale Aufnahme zeigte
   nur ~13dB Unterdrückung statt der für `hilbert_fc(401, WIN_HAMMING,
   6.76)` erwarteten 60-90dB. Isolierte Digitalketten-Tests zeigten
   durchweg 60-94dB — die Software-Kette ist sauber, die
   Verschlechterung passiert analog im AD9361-Sendepfad (IQ-Imbalance),
   dieselbe Eigenschaft wie bei FreeDV. Kein Software-Bug. Mitigation:
   `DIGITEXT_MIN_FREQ_HZ` von 300Hz auf 4000Hz angehoben, um Signal und
   Spiegelbild optisch weit genug zu trennen.
6. **FM/SSB nach Digitext-Nutzung nicht mehr erreichbar.** Der
   Mode-Tab-Wechsel war einseitig. Fix: `_on_mode_tab_changed()` jetzt
   bidirektional.
7. **Text erschien auf dem Kopf.** Zeilen wurden in natürlicher
   Bild-Reihenfolge gesendet, aber Wasserfälle scrollen mit den
   neuesten Daten oben. Fix: Zeilenreihenfolge vor dem Senden gespiegelt
   (`bitmap[::-1]`).
8. **Wiederholtes Senden desselben Texts brach vorzeitig ab.** Der
   Auto-Unkey-Watchdog identifizierte eine Übertragung nur über die
   Flowgraph-Instanz, nicht den einzelnen PTT-Druck — bei identischer
   Dauer landete der Watchdog-Timer des VORHERIGEN Drucks mitten in der
   NÄCHSTEN Übertragung. Fix: pro Tastendruck hochgezählter
   Epochen-Zähler, den beide Timer gegenprüfen.
9. **Harte Bandbreiten-Obergrenze bei ~18000Hz, langsames Ausfaden.**
   Nicht der Hilbert-Modulator (flach bis 23.8kHz gemessen), sondern
   `digitext_ssb_resampler`s Anti-Alias-Filter (`fractional_bw=0.4`).
   Fix: nur für diesen Resampler `fractional_bw` auf 0.47 angehoben —
   Passband jetzt flach bis ~22kHz.

**`errno 113` beim Pluto-Connect geklärt** (kein Code-Bug):
`plutoplus.local` löst per mDNS teils auf eine Adresse auf, die vom
Rechner aus zeitweise nicht erreichbar ist, während der Pluto über eine
direkte USB-Ethernet-Verbindung einwandfrei erreichbar bleibt. Dasselbe
Muster tauchte später nochmal bei `pluto_cli` auf (siehe dort:
intermittierend falsche mDNS-Antwort, nicht dauerhaft falsch) — Workaround
weiterhin: bei diesem Fehler die direkte IP/USB-Verbindung statt des
Hostnamens verwenden, z.B. `--uri ip:192.168.2.1`.

---

## Sonstige archivierte Hinweise

- **`pluto_rx`** (die ursprüngliche, einfachere RX-App) wurde am
  2026-09-17 vollständig entfernt — fully superseded by
  `pluto_advanced_rx`, keine Code-Referenzen mehr vorhanden.
- **FT8-Digimode**: separat geparkt in `backlog/ft8/`, siehe
  `backlog/ft8/FT8_HANDOFF.md` vor jeder Wiederaufnahme.
- **M17-Chopping-Investigation** (17-Schritte-Isolationslog) existierte
  als eigene `M17_CHOPPING_HANDOFF.md`-Datei im Repo-Root, wurde nach
  bestätigtem/committetem Fix (`19dce00`) und expliziter Nutzerfreigabe
  am 2026-09-17 gelöscht — die Kurzfassung des Fixes steht oben im
  M17-Abschnitt.

## Offene Punkte (Stand 2026-09-17, aus dem alten README übernommen)

- Mehr Digitext/Waterfall-Writer-Testwörter, andere
  Antennenaufstellungen/Abstände; ob die AD9361-IQ-Imbalance (~13-18dB
  Unterdrückung) durch Kalibrierung statt nur Frequenzverschiebung
  reduzierbar wäre.
- HackRF-„Spike"-Beobachtung beim ersten realen PTT-Test nicht
  abschließend geklärt (Zeitkorrelation unklar) — für echte Klärung:
  Aufnahme mit geloggten Zeitstempeln statt Live-Beobachtung.
- HackRFDevice hat keine von GNU Radio unabhängige Sicherheitsschicht
  wie `PlutoSafety` (bewusste Entscheidung, aktuelle
  `gnuradio.soapy`-Bindings bieten keine Grundlage dafür).
- RADE-EOO-Tail braucht eigene, isolierte Hardware-Verifikation.
- RADE Auto Fine-Tune nur grob kalibriert (ein Datenpunkt/Antennenaufbau);
  HackRF als RX-Backend für Auto-Tune ungetestet.
- SoapyRemote (Netzwerkzugriff für HackRF/RTL-SDR) nur als Fähigkeit
  dokumentiert, nicht Ende-zu-Ende getestet (kein Zweitrechner
  verfügbar).
- RADE über Soundkarte nicht mit echtem externem SSB-Funkgerät getestet.
- FreeDV `reliable_text` bei gepackter `libcodec2` (1.2.0-4) — Bug
  gefunden per `gdb`, aber upstream (drowe67/codec2) noch nicht
  gemeldet.
- Geteilter AD9361-Takt zwischen `pluto_advanced_rx`/`pluto_tx` — ein
  erster Fix-Versuch (Takt vor jedem Senden zurückholen) löste das
  Problem nicht und wurde wieder entfernt. Braucht Recherche zu
  unabhängigem RX/TX-Takt auf dem AD9361/Pluto+.
- Nativer USB-Backend ungetestet (könnte RX-Durchsatz über die
  aktuellen ~4,7-4,9 MSa/s hinaus verbessern; udev-Rechte sind bereits
  geklärt, `libiio0` braucht keinen Gruppenzwang).
- `pluto_advanced_rx`: mehrstufige IF-Dezimation für 15/20-MHz-Presets
  fehlt (aktuell ein einzelner `rational_resampler_ccf`-Schritt, der bei
  diesem Verhältnis einen zu langen Filter für den Scheduler erzeugt).
- M17-Demodulation in `pluto_advanced_rx`: per Software-Loopback
  bit-exakt bestätigt und mittlerweile auch real (Pluto TX -> RTL-SDR RX,
  432,15MHz) verifiziert — ein Cross-Check gegen eine unabhängige
  M17-Implementierung steht noch aus.
- Baseband-TX/RX-Modus: nur per synthetischem 4-Ton-Software-Loopback
  verifiziert, noch kein echter fldigi-Rundlauftest über RF.
- GUI besser/cooler aussehen lassen — aktuell rein funktionale
  Standard-Qt-Widgets.

---

## RTTY-Digimode (TX+RX, 2026-09-17) + RX-Digimode-Selector-Refactor

Neuer Digimode (2-Ton-FSK, Baudot/ITA2) neben PSK31/Waterfall Writer,
TX+RX, mit einstellbarer Baudrate/Shift/Mark-Frequenz und
Normal/Reverse. Auf Nutzerwunsch außerdem ein grundlegendes Redesign
der RX-Digimode-Architektur: PSK31 lief bisher unbedingt im Hintergrund
mit (`pluto_advanced_rx/flowgraph.py`, seit dessen Einführung) — jetzt
sind PSK31 und RTTY gegenseitig exklusiv (`AdvancedRxFlowgraph.
active_digimode`), gesteuert über ein neues RX-seitiges
`digimode_combo` (spiegelt das TX-seitige).

**Baudot-Tabelle korrekt aus einer echten Referenz übernommen**
(dl-fldigi/`rtty.cxx`, US/commercial-FIGS-Variante, nicht reine
ITU-ITA2), nach demselben Prinzip wie PSK31s Varicode-Tabelle aus
fldigis `pskvaricode.cxx`. Ein eigener, aus dem Gedächtnis
rekonstruierter Tabellenentwurf während der Planungsphase erwies sich
bei der Verifikation als teilweise falsch (u.a. T statt E bei Code 1) —
genau der Grund, warum eine zitierbare Quelle Pflicht war.

**Echter Bug beim ersten Offline-Rundlauftest gefunden: TX/RX-USOS-
Assymetrie.** RTTYs "Unshift On Space"-Konvention (Space setzt den
Empfänger-Shift-Zustand auf LTRS zurück) war nur im RX-Deframer
implementiert, nicht im TX-Encoder — TX behielt den FIGS-Zustand über
ein Leerzeichen hinweg bei (kein Shift-Code nötig, da Space in beiden
Tabellen denselben Code hat), RX setzte aber unabhängig davon nach
jedem Space auf LTRS zurück. Ergebnis: Zeichen nach "... : " wurden mit
dem falschen Tabellen-Satz dekodiert (`DGBXC` statt `$&?/:`). Fix:
TX-Encoder setzt `shift_state` jetzt ebenfalls nach jedem echten
Space-Zeichen zurück — beide Seiten folgen jetzt derselben Konvention.
32/32 Offline-Rundlauftests (4 Texte × 2 Baudraten × 2 Shifts ×
Normal/Reverse) bestehen seither.

**Präambel-Design geändert, ebenfalls durch einen Offline-Test
gefunden.** Ursprünglich wie eine klassische "RY-Diddle"-Präambel
geplant (alternierend Mark/Space) — funktionierte nicht mit dem
letztlich gewählten RX-Deframer-Design (offener UART-Stil statt
`symbol_sync_ff`, siehe unten): jede Mark→Space-Flanke der
alternierenden Präambel löste einen (meist fehlschlagenden, aber nicht
immer) Frame-Versuch aus, was das erste echte Zeichen der Nachricht
verschluckte. Fix: Präambel ist jetzt durchgehendes Idle-Mark (kein
Wechsel, keine Flanken) — genau der reale Ruhezustand einer
unbetätigten RTTY-Sendung.

**RX-Demod-Kette bewusst ohne `symbol_sync_ff`** (anders als PSK31):
Baudot-Framing ist echt asynchron (beliebig lange Idle-Mark-Lücken
zwischen Zeichen, keine Flanken) — ein kontinuierlich mitlaufender
Mueller-&-Mueller-Loop würde in solchen Lücken driften, genau dort wo
es beim nächsten Startbit am meisten schadet. Stattdessen: oversampled
Bitstrom (`RTTY_WORKING_RATE_HZ=5000`, ~40-110 Samples/Bit je nach
Baudrate) + eigener `RTTYBaudotDeframer` (`gr.sync_block`), der
Mark→Space-Flanken selbst erkennt und die 5 Datenbits + Stoppbit an
festen, aus `working_rate/baud_rate` berechneten Offsets abtastet
(klassisches Open-Loop-UART-Verfahren). Vorteil nebenbei: Baudraten-
Wechsel braucht dadurch keine GNU-Radio-Rekonfiguration, nur eine
billige Neuberechnung im Deframer selbst.

**Echte, hart erarbeitete GNU-Radio-Einschränkung gefunden: ein Block
mit Pflicht-Eingang kann nicht dauerhaft unverbunden bleiben, auch
nicht zur Laufzeit.** Der ursprünglich geplante Ansatz (ein einziger
laufender Flowgraph, `if_filter` wird per `lock()/connect()/
disconnect()/unlock()` je nach gewähltem Digimode nur zum aktiven
Zweig verbunden, nach dem Vorbild von `set_demod_mode()`) scheiterte
direkt an einem Minimalbeispiel: `unlock()` wirft `RuntimeError:
insufficient connected input ports`, sobald ein Block mit
Pflicht-Eingang (z.B. `freq_xlating_fir_filter_ccf`) komplett
unverbunden ist — nicht nur beim initialen `start()`, sondern bei jedem
späteren `lock()/unlock()`-Zyklus auf einem bereits laufenden
Flowgraph. `set_demod_mode()`s eigenes Muster funktioniert nur, weil es
NIE einen Pflicht-Eingang auf null Verbindungen bringt (es tauscht nur,
welcher von mehreren Producern einen gemeinsamen Sink speist — der
Sink hat immer genau einen). Konsequenz: Digimode-Wechsel (PSK31↔RTTY,
und Tab-Betreten/-Verlassen) bauen jetzt den kompletten
`AdvancedRxFlowgraph` neu auf (`gui.py`s `_rebuild_for_digimode()`,
exakt nach dem Vorbild von `_on_bandwidth_changed()`/
`_on_audio_device_changed()`), inkl. kurzer Audio-Unterbrechung beim
Wechsel — vom Nutzer nach Rückfrage explizit als akzeptabler
Trade-off bestätigt (Digimode-Wechsel ist eine gelegentliche,
bewusste Aktion, kein Dauerbetrieb).

Zweiter, damit verwandter Fund: das bloße Weglassen des
`if_filter`-Connects am EINGANG eines Zweigs reicht nicht — wenn der
Block trotzdem an ANDERER Stelle (z.B. als Quelle für den nächsten
Block) in einem `connect()`-Aufruf vorkommt, zählt er als Teil des
Flowgraphs und sein eigener Pflicht-Eingang wird trotzdem validiert.
Nötig war, für den inaktiven Digimode-Zweig ALLE `connect()`-Aufrufe
der gesamten Kette wegzulassen, nicht nur den ersten — ein Block, der
buchstäblich in KEINEM `connect()`-Aufruf vorkommt, ist dagegen
nachweislich komplett aus dem laufenden Flowgraph ausgenommen (per
direktem Test bestätigt) und kostet nichts.

**AFC für RTTY bewusst anders als PSK31s Design**: PSK31 sucht mit
einer einzigen breiten, leistungsgewichteten Centroid-Suche um den
Nominalwert. Für RTTY mit zwei separaten Tönen ungeeignet (Mark/Space-
Energieverhältnis ist inhaltsabhängig — LTRS-lastiger vs.
FIGS/Ziffern-lastiger Verkehr verschiebt, welcher Ton gerade
dominiert), deshalb zwei getrennte schmale Suchen (eine um Mark, eine
um die aktuelle Space-Position) mit anschließender Mittelung; die
Suchradius skaliert mit der konfigurierten Shift statt eines festen
Werts wie bei PSK31 (RTTYs belegte Bandbreite variiert 3× zwischen den
Shift-Presets).

**Real auf Hardware verifiziert** (Pluto TX → RTL-SDR RX, 432,15MHz,
-20dB Leistungsdeckel, über `pluto-cli tx rtty`/`pluto-cli rx fm
--digimode rtty`): gesendeter Text `DA2JH PLUTO-CLI RTTY TEST` wurde im
hinteren Teil exakt korrekt dekodiert ("...CLI RTTY TEST" fehlerfrei),
der Anfang war verunstaltet — deckt sich mit dem bereits für PSK31
dokumentierten, bekannten Phänomen einer kontinuierlich driftenden
TX/RX-Frequenzabweichung dieses konkreten Pluto+RTL-SDR-Paars, die die
AFC erst nach ein paar Polling-Zyklen einholt. Kein neuer Bug, sondern
dieselbe bereits bekannte Hardware-Eigenart.

`pluto_cli/rx.py`s `--psk31-monitor`/`--psk31-tone-hz` wurden durch ein
einziges `--digimode {psk31,rtty}` plus modusspezifische Flags ersetzt
(bewusster Breaking Change) — der alte Flag gatete ohnehin nur die
STDOUT-Ausgabe, nicht den Flowgraph selbst (PSK31 lief davor sowieso
immer mit), was mit dem neuen `active_digimode`-Design inkonsistent
gewesen wäre.
