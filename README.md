# pluto-tx

Eigene Sende- und Empfangssoftware für den ADALM-PLUTO (Pluto+,
Tezuka-Firmware), HackRF One und RTL-SDR, gebaut mit GNU Radio —
FM/SSB-Sprechfunk, mehrere Digitalsprache-Modi (M17, FreeDV 2020/2020B,
RADE), Digimodes (Waterfall Writer, PSK31, RTTY, POCSAG, Meshtastic/LoRa, MeshCore/LoRa) und ein wiederholender
Datei-Broadcast. Entstanden, weil vorhandene TX-Software (SDRangel) den
AD9361-Sendezweig nach "Stop" aktiv weitersenden ließ — dieses Projekt
legt deshalb besonderen Wert auf eine eigene, von GNU Radio unabhängige
Sicherheitsschicht (siehe [Sicherheit](#sicherheit)).

Drei Programme, ein Repo:

- **`pluto-tx`** — Sende-App (GUI)
- **`pluto-advanced-rx`** — Empfänger-App mit interaktivem, SDR++-artigem
  Wasserfall (GUI)
- **`pluto-cli`** — headless Kommandozeilen-Zugriff auf die wichtigsten
  Funktionen beider Apps, für Skripte/Automatisierung ([volle
  Referenz](pluto_cli/README.md))

**Sendebetrieb nur mit gültiger Amateurfunklizenz.** Verantwortung für
Frequenzwahl, Bandplan und Sendeleistung liegt beim Betreiber.

## Auf einen Blick

| | |
|---|---|
| **TX-Modi** (`pluto-tx`) | FM (optional mit CTCSS/DCS), SSB (USB/LSB), M17, FreeDV 2020/2020B, RADE V1, File Broadcast, Baseband |
| **RX-Modi** (`pluto-advanced-rx`) | FM, SSB (USB/LSB), RADE V1, M17, Baseband |
| **Digimodes** (beide Apps, eigener Reiter) | Waterfall Writer (Text im Wasserfall), PSK31-Chat, RTTY, POCSAG (Funkruf), Meshtastic (LoRa; nur Pluto/HackRF/RTL-SDR), MeshCore (LoRa, nur RF-Geräte) |
| **Hardware** | PlutoSDR/Pluto+ (TX+RX), HackRF One (TX+RX), RTL-SDR (RX), Soundkarte/externes Funkgerät (TX+RX) |
| **Automatisierung** | `pluto-cli` — dieselbe Codebasis headless, `--json`-Ausgabe |
| **Sicherheit** | NOTAUS, von GNU Radio unabhängige Abschalt-Logik, Live-Hardware-Readout |

<p align="center">
  <img src="docs/screenshots/pluto-tx-audio.png" alt="pluto-tx: FM-Sendung läuft, TX-Basisband-Wasserfall, Sicherheits-Readout" width="49%">
  <img src="docs/screenshots/pluto-advanced-rx-waterfall.png" alt="pluto-advanced-rx: interaktiver Wasserfall mit echtem Empfang" width="49%">
</p>

## Installation

```
./install.sh
```

Für Debian/Ubuntu (`apt-get`). Installiert `gnuradio` (zieht `gr-iio` und
`python3-pyqt5` mit), `python3-libiio`/`libiio-utils` (TX-Sicherheitsschicht
+ Fehlersuche), `avahi-daemon` (löst `*.local`-Hostnamen wie
`plutoplus.local` auf), `python3-pyqtgraph` (Wasserfall), HackRF-/
RTL-SDR-Unterstützung (`soapysdr-module-hackrf`/`-rtlsdr`, `hackrf`,
`rtl-sdr`, `python3-soapysdr`), `python3-pil`/`fonts-dejavu-mono`
(Waterfall Writer) und `git`. Prüft danach per echtem
Python-Import, ob alles verfügbar ist, und legt `~/.local/bin/pluto-tx`,
`pluto-advanced-rx`, `pluto-cli` an. Beliebig oft wiederholbar.

Der Pluto muss per Netzwerk (`ip:...`) oder USB (`usb:...`) erreichbar
sein — für USB ist nichts weiter nötig. Für HackRF/RTL-SDR richtet das
Skript zusätzlich ein: den aktuellen User zur Gruppe `plugdev`
hinzufügen (**wirkt erst nach Neu-Login**) und den Kernel-DVB-T-Treiber
`dvb_usb_rtl28xxu` blacklisten (RTL-SDR-Sticks werden sonst automatisch
vom Kernel als TV-Tuner beansprucht — ein bereits gestecktes Gerät
braucht dafür ein Aus-/Einstecken oder `sudo rmmod dvb_usb_rtl28xxu`).

### Optional: M17

```
./install-m17.sh
```

`install.sh` installiert bewusst kein `gnuradio.m17` (kein apt-Paket,
braucht C++/CMake-Build, [gr-m17](https://github.com/M17-Project/gr-m17)).
Ohne dieses Skript ist der M17-Moduseintrag in beiden Apps ausgegraut,
der Rest läuft normal. Baut `gr-m17` (gepinnter Commit) nach
`$HOME/.local`, regeneriert die Starter mit passendem `LD_LIBRARY_PATH`.

### Optional: Meshtastic (LoRa)

```
./install-lora.sh
```

Baut [`gr-lora_sdr`](https://github.com/tapparelj/gr-lora_sdr) (GPL-3.0,
gepinnter Commit) nach `$HOME/.local` und regeneriert die Starter. Die
Protokollschicht braucht zusätzlich die pip-Pakete `meshtastic` und
`cryptography` (`pip install --user --break-system-packages meshtastic
cryptography` bei PEP-668-Systemen). Ohne das ist der
Meshtastic-Eintrag im Digimode-Kombo beider Apps ausgegraut, der Rest
läuft normal. **MeshCore** braucht nur `gr-lora_sdr` und `cryptography`
(nicht das `meshtastic`-Paket).

### FreeDV braucht kein separates Skript

`libcodec2` mit LPCNet/2020/2020B-Support ist bereits transitive
Abhängigkeit von `gnuradio` — FreeDV funktioniert direkt nach
`install.sh`.

### Optional: RADE V1

```
./install-rade.sh
```

Ohne dieses Skript ist der RADE-Moduseintrag in beiden Apps ausgegraut,
der Rest läuft normal. Baut [`freedv/rade_c`](https://github.com/freedv/rade_c)
(gepinnter Commit) nach `rade_c/` im Projektverzeichnis und regeneriert
alle drei Starter mit passendem `LD_LIBRARY_PATH`/`PATH`.

## Erste Schritte

```
pluto-tx
pluto-advanced-rx
```

Start verbindet automatisch mit `config.DEFAULT_URI` (`ip:plutoplus.local`).
Läuft der Verbindungsaufbau nicht sofort durch (z.B. `errno 113`/"No
route to host" bei mDNS-Problemen), im Device-Feld stattdessen die
direkte IP eintragen, z.B. `ip:192.168.2.1` (USB-Ethernet-Gadget-IP des
Pluto), und "Connect" klicken.

**Erste FM-Verbindung senden**: `pluto-tx` starten, Frequenz eintragen
(z.B. `432.1500` MHz im 70cm-Band), TX Power niedrig lassen (Standard-
Ceiling ist bereits konservativ), Mikrofon oder eine Testdatei
(`da2jh-test.wav` ist voreingestellt) als Quelle wählen, PTT-Knopf
drücken und halten. Ein zweites Gerät (RTL-SDR mit `pluto-advanced-rx`,
oder jeder andere Empfänger) sollte das Signal bei der eingestellten
Frequenz zeigen.

**M17 direkt gestartet (nicht über den `pluto-tx`-Starter) bleibt
ausgegraut** — `LD_LIBRARY_PATH` wird nur vom generierten Starter
gesetzt:

```
export LD_LIBRARY_PATH="$HOME/.local/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
```

## Beispielhafte Anwendungsfälle

**FM/SSB-Sprechfunk.** Klassischer Sprechfunk über Pluto oder HackRF —
Mikrofon oder Audiodatei als Quelle, Noise-Gate/Kompressor/Limiter
optional zuschaltbar (siehe [Sicherheit](#sicherheit) für den
PTT-Sicherheitsmechanismus dahinter). Im FM-Modus kann ein **CTCSS-Ton**
(50 Standardtöne, 67–254,1 Hz) oder ein **DCS-Code** (83 Standardcodes, Polarität
N/I) unhörbar mitgesendet werden — z. B. um ein Relais zu öffnen; Pegel in % des
Hubs einstellbar (Standard 12 %, die Stimme wird um denselben Anteil leiser, der
Gesamthub bleibt gleich). Der Ton läuft ab PTT-Druck; dem Empfänger ~0,3 s
Vorlauf geben, bevor gesprochen wird. Ein Squelch-Tail (Reverse-Burst bzw.
DCS-Abschaltcode) wird nicht gesendet. SSB gibt es als USB und LSB
(HF-Konvention: unter 10 MHz LSB); die Digimodes (PSK31, RTTY, …) bleiben USB.
CLI: `pluto-cli tx fm --ctcss 88.5` bzw. `--dcs 023` / `--dcs 754I`,
`pluto-cli tx lsb`, `pluto-cli rx lsb`.

**Digitalsprache mit M17.** `pluto-tx` sendet, jeder M17-fähige
Empfänger (auch `pluto-advanced-rx` selbst, oder SDR++ mit dessen
eigenem M17-Demodulator) kann mitlesen/mithören. Rufzeichen (Quelle/
Ziel) direkt in der GUI einstellbar.

**PSK31-Chat.** Klassisches Keyboard-to-Keyboard-Digimode, eigener
Reiter "Digimodes" in beiden Apps. Läuft unabhängig vom gerade
gewählten primären Empfangsmodus (man kann z.B. FM hören und
gleichzeitig einen PSK31-Chat auf derselben Bandbreite mitverfolgen) —
aber nur einer der Digimodes (PSK31, RTTY, POCSAG **oder** Meshtastic) kann
gleichzeitig aktiv dekodieren, per eigenem Digimode-Kombo im
Digimodes-Reiter umschaltbar.

**Wiederholte Aussendung.** Im Digimodes-Reiter der TX-App (Waterfall Writer,
PSK31, RTTY, POCSAG, Meshtastic) gibt es „Repeat“: Anzahl der Aussendungen (1–999)
und Intervall in Sekunden (Pause zwischen Ende der einen und Beginn der nächsten
Aussendung, der Sender ist dazwischen aus). PTT startet die Serie, ein erneuter
Klick auf PTT beendet sie sofort, auch während der Pausen; E-STOP, Trennen und
Moduswechsel ebenfalls. Vor jeder Wiederholung laufen die Prüfungen erneut
(z. B. Meshtastic-Duty-Cycle, leerer Text): schlägt eine fehl, endet die Serie.
CLI: `--repeat-count N --repeat-interval SEKUNDEN` bei `tx digitext|psk31|rtty|pocsag|meshtastic`.

**RTTY.** Klassisches 2-Ton-FSK-Fernschreiben (Baudot/ITA2), ebenfalls
im Digimodes-Reiter. Baudrate (45.45/50/75/100) und Shift
(170/425/850Hz) frei einstellbar, inkl. Normal/Reverse-Umschalter für
Gegenstationen mit vertauschter Ton-Zuordnung.

**POCSAG (Funkruf).** Standardkonformer POCSAG-Digimode nach ITU-R M.584
(2-FSK, ±4,5 kHz Hub, 512/1200/2400 Bit/s, Logik 1 = niedrigere Frequenz), mit
Pluto/HackRF/RTL-SDR und über die Soundkarte. Soundkarte: TX gibt das geformte
NRZ-Signal (±1, Pegel 0,7) am Ausgang aus, um es in den Daten-/Mikrofoneingang eines
FM-Funkgeräts zu speisen (das Gerät moduliert, Eingangspegel auf ±4,5 kHz Hub
einstellen; DC-gekoppelter Datenanschluss ist ideal, ein wechselstromgekoppelter
Mikrofoneingang verformt lange gleiche Bitfolgen); RX liest das demodulierte
Diskriminator-/Datensignal eines Funkgeräts vom Audio-Eingang (Pegel beliebig, AGC).
**RX:** Kanalmitte einstellen, alle drei Baudraten
werden parallel dekodiert (Autobaud, beide Polaritäten, BCH-Fehlerkorrektur bis
2 Bit); Tabelle mit Zeit, Baud, RIC, Funktion, Text und Fehleranzeige,
Darstellung „Auto/Alpha/Numerisch“ und optional deutscher Zeichensatz
(DIN 66003). **TX:** Aussendung eines Rufs an eine RIC (0–2097151; Vorgabe ist
eine Test-RIC) als Alpha-, Numerisch- oder Nur-Ton-Nachricht mit Funktionsbits
0–3, Direktmodulation des Trägers mit Gauß-geformtem NRZ. Nur in Amateurfunk-
bändern senden (nicht auf kommerziellen Funkrufkanälen oder dem DAPNET-Kanal).
Die Inhalte fremder, nichtöffentlicher Funkrufe sind nicht für den Empfänger
bestimmt: die App zeigt sie an, speichert sie aber nicht.
CLI: `pluto-cli tx pocsag --ric 1234567 --text "DA2JH Test" --baud 1200`,
`pluto-cli rx fm --digimode pocsag`.

**Meshtastic (LoRa).** Sendet und empfängt echte Meshtastic-Pakete
(alle neun Modem-Presets LongFast … ShortTurbo, je EU433/EU868; Träger und
Standard-Kanalname wie in der Firmware berechnet) im Digimodes-Reiter beider
Apps. **Nur LongFast (SF11/250 kHz) ist bit-genau gegen einen echten Heltec V3
verifiziert** (868 MHz, beide Richtungen); die übrigen Presets stammen aus der
Firmware-Tabelle, ihre Sync-Symbole sind extrapoliert (im Programm markiert).
`pluto-tx` sendet eine Textnachricht als Broadcast auf dem Standardkanal
(Kanalname/PSK, Hop-Limit und Node-ID einstellbar, PTT sendet genau ein
Paket und löst danach selbst aus); `pluto-advanced-rx` zeigt jedes
empfangene Paket in einer Tabelle (Absender, Ziel, Typ, Hops, Text —
Pakete anderer Kanäle als "other channel"). Nur ein Digimode gleichzeitig
(wie bei PSK31/RTTY), und nur mit RF-Gerät (Pluto/HackRF/RTL-SDR),
nicht über Soundkarte.

**MeshCore (LoRa).** Eigener Digimode neben Meshtastic (gleiche LoRa-Basis,
anderes Paketformat). Preset „MeshCore EU/UK Narrow“: 869,618 MHz, SF8, 62,5 kHz,
CR 4/8, 32 Präambel-Symbole, Sync 0x12 — **bit-genau gegen einen echten Heltec V3
verifiziert** (RX dekodiert alle acht aufgezeichneten Bursts byte-gleich zum
ESP32-Referenz, unsere TX-Chirp-Symbole stimmen mit dem echten Burst überein).
`pluto-advanced-rx` zeigt Adverts (Name, Rolle, Position, **Ed25519-Signatur
geprüft**), entschlüsselt Gruppentexte des öffentlichen Kanals (und weiterer
Kanäle mit eigenem 32-Hex-Schlüssel; AES-128-ECB + 2-Byte-HMAC, MAC geprüft) und
führt eine Knotenliste; andere Pakettypen erscheinen mit Typ, Route, Pfad und
Länge als „unverifiziert“ (ausblendbar). Nichts davon wird gespeichert.
`pluto-tx` sendet einen signierten **Advert** (Flood/Direct, Name, Rolle, optional
Position) oder einen **Gruppentext** (Public oder eigener Kanal); die App ist ein
eigener neuer MeshCore-Knoten (Ed25519-Schlüssel wird beim ersten Mal in
`~/.config/pluto-tx/meshcore_identity.json` angelegt, Rechte 0600, nie den Schlüssel
eines anderen Geräts benutzen). 10 % Duty-Cycle wird durchgesetzt. **In
Amateurfunkbändern nur Adverts** (MeshCore-Text ist immer verschlüsselt, und der
Advert braucht dort einen Namen = Rufzeichen). Ein Flood-Advert wird von
Repeatern weiterverteilt — Sendetests zunächst gegen einen isolierten Empfänger.
Direktnachrichten (TXT_MSG) sind noch nicht implementiert.
CLI: `pluto-cli tx meshcore --kind advert --name DA2JH`, `pluto-cli rx fm --digimode meshcore`.
**Rechtlicher Hinweis, im Programm bei jedem Preset sichtbar:**
*433 MHz* (Band 433,0–434,0 MHz) liegt im deutschen 70-cm-Amateurfunkband — dort greift die
Eigenbau-Ausnahme, es gilt aber Ham Mode (Rufzeichen Pflicht, keine
Verschlüsselung; die App erzwingt beides). *869,525 MHz* ist reines
ISM/SRD-Band ohne Amateurfunk-Sonderrecht: der Betrieb unzertifizierter
SDR-Hardware ist dort ein eigenes, bewusst zu tragendes Risiko; die App
erzwingt wenigstens die 10 % Duty-Cycle pro Stunde (Sendungen werden
sonst abgelehnt). Zum Testen `--hop-limit 0` und geringe Leistung
verwenden, kein Rufzeichen in Text auf dem öffentlichen 868-MHz-Kanal.

**Waterfall Writer.** Sendet eingegebenen Text so, dass er beim
Empfänger direkt im Wasserfall/Spektrum als lesbares Bild erscheint —
dieselbe Grundidee wie klassisches Hellschreiber. Zwei Layouts
(horizontal/vertikal), Zoom-Faktor und Frequenzoffset einstellbar.

**File Broadcast.** Eine oder mehrere Dateien werden wiederholend
gesendet (Round-Robin, mit Gap-Filling) — praktisch für z.B.
Notfunk-Bulletins oder Community-Downloads, bei denen Empfänger
jederzeit einsteigen können, ohne den Sendebeginn abzuwarten.
`pluto-advanced-rx` empfängt File Broadcast ebenfalls immer parallel im
Hintergrund und speichert vollständig empfangene Dateien automatisch.

**RTL-SDR über das Netzwerk.** Ein RTL-SDR an einem anderen Rechner (z.B.
Raspberry Pi am Dachboden) läuft dort über den Standard-Server
`rtl_tcp -a 0.0.0.0 -p 1234` (Paket `rtl-sdr`). In `pluto-advanced-rx` den
Gerätetyp „RTL-SDR“ wählen und im Verbindungsfeld neben dem Scan-Button
`192.168.178.34:1234` (oder nur die IP, Port-Default 1234) eintragen —
Serien­nummern für lokale USB-Sticks funktionieren wie bisher. Gleich per
CLI: `pluto-cli rx fm --device rtlsdr --uri 192.168.178.34:1234`. Hinweise:
`rtl_tcp` bedient nur einen Client gleichzeitig und hat keine
Authentifizierung (nur im vertrauenswürdigen LAN betreiben); bei 2,4 MS/s
fließen ~4,8 MB/s, über WLAN eine niedrigere RX-Bandbreite wählen (gemessen: bei 2,4 MS/s
verliert die WLAN-Strecke Daten, bis ~1 MS/s ist sie verlustfrei).
`rtl_tcp` liefert den Strom in 256-kB-Blöcken (bei niedrigen Raten Pausen bis
~0,5 s); ein selbstanpassender Puffer glättet das und hält den Ton
ruckelfrei, kostet aber entsprechend Latenz (Abstimmen wirkt ~0,5–1 s
verzögert; Puffer/Unterläufe stehen im HW-Status). Bricht die
Verbindung ab, verbindet sich die App selbst neu und sendet die Einstellungen
erneut (Statuszeile zeigt „lost“/„restored“).

**Frequenzkorrektur (ppm) und Direct Sampling.** Beide Apps haben für alle
RF-Geräte (Pluto, HackRF, RTL-SDR) ein Feld „Freq. correction (ppm)“, Default 0.
Der Wert ist die **Abweichung des Geräts** (wie bei rtl_test/Kalibrate/gqrx):
positiv = das Gerät läuft zu hoch, die App stimmt die Hardware entsprechend
tiefer ab. Da es ein Verhältnis ist, skaliert eine einmal bekannte Abweichung
mit der Frequenz (Beispiel: zeigt ein Empfänger deinen 432,150-MHz-Träger bei
432,125 MHz, ist das Gerät ~58 ppm zu tief → −58 eintragen). In
`pluto-advanced-rx` steht die Zeile direkt unter der Geräteauswahl; für den
RTL-SDR gibt es dort zusätzlich **Direct sampling** (Tuner wird umgangen, HF
0,1–28,8 MHz (über 14,4 MHz aliased: das Band mischt sich mit dem Spiegelbild von 28,8 MHz − f, Bandpass empfehlenswert), Dropdown Q-/I-Zweig — je nach Verdrahtung des HF-Eingangs, beim
RTL-SDR Blog V3 Q); beim Einschalten wechselt der Frequenzbereich auf HF, beim
Ausschalten kommt die vorherige Frequenz zurück. Der Tuner-Gain wirkt im
Direct-Modus nicht. AM (typisch für HF) kann die RX-App noch nicht; LSB gibt es als eigenen Modus „SSB (LSB)“.

**Baseband-Modus + fldigi.** Roher, unverarbeiteter Audio-Durchgriff
(kein Noise-Gate/Kompressor/Limiter, breiter als normales 3kHz-SSB-
Audio) — macht beide Apps zu einem Input/Output für externe
Digimode-Software wie fldigi, verbunden über die eingebauten,
persistenten PipeWire-Loopback-Knoten. Details und ein vollständiges
Rezept in [`pluto_cli/README.md`](pluto_cli/README.md#7-recipes).

**Automatisierung via `pluto_cli`.**

```
pluto-cli tx fm --freq 432150000 --yes
pluto-cli rx m17 --freq 432150000 --json
pluto-cli devices scan --tx --device pluto
```

Eigener, headless Prozess — importiert `pluto_tx`/`pluto_advanced_rx`
statt sie zu kopieren, kein Qt-Event-Loop, läuft auch über SSH/Cron.
Vollständige Referenz (alle Modi/Flags, JSON-Schema, Sicherheitsmodell,
weitere Rezepte) in [`pluto_cli/README.md`](pluto_cli/README.md).

## Unterstützte Hardware

| Backend | `pluto-tx` (TX) | `pluto-advanced-rx` (RX) |
|---|---|---|
| **PlutoSDR / Pluto+** | alle Modi, volle Sicherheitsschicht (Dämpfung + LO-Powerdown, siehe unten) | alle Modi |
| **HackRF One** | alle Modi | alle Modi |
| **RTL-SDR** (USB oder per `rtl_tcp` im Netzwerk) | — (kein TX-fähiges Gerät) | alle Modi |
| **Soundkarte / externes Funkgerät** | RADE, Waterfall Writer, PSK31, RTTY, POCSAG | alle Modi (Audio Input, z.B. für RADE über ein SSB-Funkgerät) |

Geräteauswahl per editierbarem Dropdown ("Device") plus Scan- und
Connect/Disconnect-Buttons. **Nur `pluto-tx`** hat zusätzlich ein
"Device Type"-Dropdown (PlutoSDR/HackRF/Soundcard), das Verbindungsfeld,
Frequenzbereich und Pegel-Regler passend umschaltet.

## Sicherheit

Der AD9361-Treiber (libiio) trennt Buffer-Streaming von Dämpfung
(`hardwaregain`) und LO-Zustand (`powerdown`) — keine getestete
SDR-Software setzt diese beim Stoppen automatisch zurück (der konkrete
Auslöser für dieses Projekt: SDRangel ließ nach "Stop" einen Träger
aktiv weiterlaufen). `pluto-tx` verwaltet TX-Dämpfung und LO-Powerdown
deshalb komplett unabhängig von GNU Radio über rohes `python3-libiio`.
Eine einzige Funktion (`force_safe_state()`: Dämpfung auf Minimum + LO
aus) ist das, worauf jeder denkbare Shutdown-Pfad läuft — normales
Programmende, Fenster schließen, SIGINT/SIGTERM, unbehandelte
Exceptions. Die GUI zeigt alle 500ms den tatsächlichen Hardware-Zustand
an (nicht nur den zuletzt gesendeten Sollwert) und hat einen
NOTAUS-Button.

PTT ist in jedem Modus und auf jedem Gerät hart mit dem tatsächlichen
HF-Abschalten verknüpft, nicht nur mit einer Pegelabsenkung: bei Pluto
zusätzlich LO-Powerdown (reine Dämpfung unterdrückt LO-Leckage nicht
ausreichend), bei HackRF ein Neuaufbau der Geräteverbindung (Nullen der
Gain-Register allein stoppt die Sendung nachweislich nicht).

## Struktur

```
install.sh / install-m17.sh / install-rade.sh / install-lora.sh
pluto_tx/                 # Sende-App
├── config.py                # geräteunabhängige Konstanten
├── devices/                  # TX-Geräte-Abstraktionsschicht (Pluto/HackRF/Soundcard)
├── safety.py                 # PlutoSafety: rohes python3-libiio, unabhängig von GNU Radio
├── flowgraph.py               # PlutoTxFlowgraph: die gesamte Signalkette
├── dynamics.py                # Kompressor/Limiter
├── lora.py / lora_airtime.py    # LoRa-PHY (gr-lora_sdr), Airtime-Formel + Duty-Cycle-Begrenzer
├── meshtastic_codec.py         # Meshtastic-Paketformat (Header, AES-CTR, Protobuf)
├── meshcore_codec.py / meshcore_identity.py  # MeshCore: Paketformat, Adverts (Ed25519), Gruppentext (AES-ECB+HMAC), Knotenidentität
├── pocsag.py / pocsag_codec.py  # POCSAG: NRZ-Audio, Codewörter/BCH/Batches/Decoder (auch von der RX-App genutzt)
├── gui.py / app.py
└── da2jh-test.wav              # Standard-Testaufnahme
pluto_advanced_rx/         # Empfänger-App mit interaktivem Wasserfall
├── devices/                  # RX-Geräte-Abstraktionsschicht (Pluto/HackRF/RTL-SDR/Audio)
├── waterfall_widget.py         # eigenes pyqtgraph-Wasserfall-Widget
└── ...
tests/                     # Unit-/Loopback-Tests: python3 -m unittest discover tests
pluto_cli/                 # headless CLI, importiert die beiden Apps oben, siehe pluto_cli/README.md
docs/screenshots/          # Screenshots für dieses README
backlog/                   # geparkte/archivierte Arbeit (Entwicklungshistorie, nicht aktiv genutzte Module)
```

`pluto_advanced_rx` ist unabhängig von `pluto_tx` (RX kann nicht senden,
braucht keine TX-Sicherheitsschicht), teilt sich aber Bandplan-/
URI-Konstanten mit ihm.

Der Wasserfall in `pluto-advanced-rx` ist ein eigenes
[pyqtgraph](https://www.pyqtgraph.org/)-Widget statt GNU Radios
`qtgui.waterfall_sink_c`: Live-Spektrum gekoppelt mit dem Wasserfall,
Klick-zum-Tunen, Tuning-Marker, Demod-Bandbreiten-Anzeige, Zoom/Pan,
einstellbare Floor/Ceiling-Slider. RX-Bandbreiten-Presets bis 10 MHz
(1/2,5/5/8/10 MHz).

## Bekannte Einschränkungen

- **Meshtastic ist ein Basis-Digimode, kein vollwertiger Mesh-Knoten**:
  gesendet wird eine Textnachricht als Broadcast (kein NodeInfo/Position,
  keine Empfangsbestätigungen, kein Weiterleiten fremder Pakete), nur der
  ein Kanal gleichzeitig. Bit-genau verifiziert ist nur LongFast auf 868 MHz
  gegen einen echten Heltec V3; andere Presets und 433 MHz (Ham Mode) wurden
  bisher nur im Selbst-Loopback getestet. Bei benutzerdefiniertem Kanalnamen
  wählt die Firmware einen anderen Frequenz-Slot — dann Frequenz von Hand
  einstellen. Ein Presetwechsel mit anderer SF/BW baut die TX-Kette neu auf.
- **Datei-Wechsel** ("Choose File") baut den Flowgraph komplett neu auf
  (kein Live-Swap in dieser GNU-Radio-Version) — kurze, aber sichere
  Unterbrechung. Audiodatei loopt unabhängig von PTT weiter, Position
  wird bei PTT nicht zurückgesetzt.
- **`plutoplus.local` kann per mDNS zeitweise auf eine nicht
  erreichbare Adresse auflösen**, während der Pluto über eine direkte
  IP (z.B. `192.168.2.1` bei USB-Ethernet-Gadget) einwandfrei erreichbar
  bleibt. Bei `errno 113`/"No route to host": direkte IP statt Hostname
  verwenden, oder `iio_info -u ip:plutoplus.local`/den GUI-Scan-Button
  zur Kontrolle nutzen.
- **Der AD9361 teilt sich einen Takt zwischen RX und TX**: läuft
  `pluto-advanced-rx` gleichzeitig mit `pluto-tx` und ändert ihre
  RX-Bandbreite, verschiebt das messbar den tatsächlich gesendeten Takt.
  RX allein ist nicht betroffen. Workaround bis zu einer echten Lösung:
  RX-Bandbreite nicht während aktiver Sendung ändern.
- **5/10 MHz RX-Bandbreite nur eingeschränkt nutzbar**: die
  `ip:plutoplus.local`-Netzwerkverbindung (IIOD-Protokoll) schafft nur
  ~4,7-4,9 MSa/s, darüber gibt es Overruns/abgehacktes Audio.
- **FreeDV-Rufzeichen/Stationskennung ist dauerhaft deaktiviert** — ein
  bestätigter Bug in der gepackten `libcodec2` (1.2.0-4) führt beim
  Verknüpfen des Textkanals zu einem reproduzierbaren Absturz.
  Stationskennung bis auf Weiteres per Sprache vor/nach der
  Übertragung.
- **Sichtbares Seitenband-/Spiegelsignal bei einigen Digitalmodi**
  (FreeDV, Waterfall Writer) — die digitale Basisbandkette misst
  offline sauber (>60dB Unterdrückung), die reale Analogkette des
  AD9361 (IQ-Imbalance) zeigt auf echter Hardware deutlich weniger.
  Kein Software-Bug, nicht per Software behebbar; Waterfall Writer
  weicht dem durch einen einstellbaren Frequenzoffset aus.

Mehr Details und die volle Bug-Historie: siehe
[`backlog/DEVELOPMENT_HISTORY.md`](backlog/DEVELOPMENT_HISTORY.md).

## Mitmachen / Weiterentwicklung

Issues und Pull Requests willkommen. Die ausführliche
Entwicklungsgeschichte — jeder gefundene Bug, jede Verifikation, interne
Architektur-Details — steht in
[`backlog/DEVELOPMENT_HISTORY.md`](backlog/DEVELOPMENT_HISTORY.md);
offene Punkte stehen am Ende dieser Datei.

## Lizenz

[GPLv3](LICENSE) — Copyright (C) 2026 Jochen Hammes (DA2JH).

Die optionalen Abhängigkeiten `gr-m17` (GPLv2) und `rade_c`/RADE
(BSD-2-Clause) werden von `install-m17.sh`/`install-rade.sh` separat aus
ihren jeweiligen Quellen gebaut, nicht in diesem Repo vendort.

## Danksagungen

- [gr-m17](https://github.com/M17-Project/gr-m17) / [M17-Project](https://m17project.org/) — offener Digitalsprache-Standard
- [freedv/rade_c](https://github.com/freedv/rade_c) — RADE (Radio Autoencoder)
- [FreeDV](https://freedv.org/) / [codec2](https://github.com/drowe67/codec2) — HF-Digitalsprache
- [GNU Radio](https://www.gnuradio.org/) — das Fundament, auf dem alles hier aufbaut
