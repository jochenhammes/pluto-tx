# JTTY — Protokoll und Anbindung an `wsjt-codec`

JTTY ist der Tastaturmodus aus WSJT-X 3.2: wie RTTY jederzeit sendbar, aber mit
Fehlerkorrektur. pluto-tx baut davon nichts nach. Packer, Fehlerkorrektur,
Signalform und Decoder bleiben die des WSJT-X-Projekts; wir sprechen dessen
Programm `wsjt-codec` über stdin und stdout an.

## 0. Quelle

| | |
|---|---|
| Repository | https://github.com/WSJTX/wsjtx |
| Tag / Commit | **v3.3.0-beta5** / `d89fe30ca1dfd3c38def8f17228f09f042829f95` (28.10.2026) |
| Lizenz | GPLv3 (wie pluto-tx) |
| Binary | `wsjtx-3.3.0-beta5-linux-{x86_64,aarch64}-codec.tar.gz`, SHA256 in `packaging/components.lock` |
| Voraussetzung | glibc ≥ 2.38 (Ubuntu 26.04 hat 2.43) |

**Warum diese Version?** Eine stabile Version mit JTTY gibt es noch nicht. `v3.2.0-rc1`
hat keinen Codec-Stream, und `lib/jtty/jtty_source_codec.f90` wurde danach noch
geändert. Das installierte WSJT-X-Paket 3.0.2 bleibt unberührt, weil sein `jt9` der
FT8-Decoder der App ist.

**Bei einer neuen Version** schlagen die Prüfsummen in `tests/test_jtty_codec.py` an,
wenn sich das Signal ändert. Das Protokoll hat keinen Versionsmarker; dann muss
dieser Text gegen den Quelltext geprüft werden.

## 1. Signal

Quelle: `lib/jtty/jtty_source_encoding.txt`, `lib/jtty/jtty_fec_mod.f90`,
`lib/jtty/gen_jttywave.f90`.

| | |
|---|---|
| Modulation | 4-GFSK, BT 2,0, Tonabstand gleich Baudrate |
| Baudrate | 12000/384 = 31,25 Bd |
| Rahmen | 13 Sync- und 46 Datensymbole, 59 × 32 ms = 1,888 s |
| Bandbreite | 125 Hz (Halbwert), 127 Hz (99 % der Leistung) |
| Nutzlast | 34 Bit: 32 Grammatik, 1 reserviert (0), 1 Nachrichtenende |
| Schutz | CRC-12, Faltungscode Rate 1/2, K = 10, tail-biting; 92 codierte Bit |
| Sync | `[0,2,2,3,0,0,3,2,1,3,1,2,0]` |
| Länge | 1–16 Rahmen, Empfänger behält 80 Zeichen |
| Zeitbezug | keiner; Sendungen beginnen jederzeit |

**Zeichensatz:** `0-9 A-Z`, Leerzeichen und `+-./?!"#$%,&*()_'=[]{}<>|:;`. Kleinbuchstaben
werden groß, alles andere wird `#`. Ein Freitext-Rahmen fasst 5 Zeichen. Rufzeichen,
Rapporte, Locator und Kontrollwörter werden vom Packer kompakter gesetzt, so belegt
`CQ DA2JH CQ` nur einen Rahmen.

**Frequenzen:** `JTTY_DIAL_FREQS_HZ` kommt aus `models/FrequencyList.cpp` dieses Tags
(USB-Dial, vorläufig). Dort steht zum Beispiel 14,090 MHz. Abweichende Listen im Netz
beziehen sich auf den rc1.

## 2. Stream-Protokoll

Quelle: `lib/streaming_io.f90`, `lib/streaming_jtty.f90`.

- stdin: 8 Byte Kopf `WSJT`, Format 0 (int16), 1 Kanal, 12 (kHz, LE16); dann Frames
  `[Typ:1][Länge:4 LE][Inhalt]` mit Typ 1 = Audio, 2 = JSON.
- stdout: je Zeile ein JSON-Objekt, unbekannte `t` werden ignoriert.
- Der Decoder sucht `rxfreq ± ntol` und zwei 300-Hz-Kanäle um 1350 und 1650 Hz, in
  `nfa..nfb` geschoben, **nicht das ganze Band**. Über etwa 2950 Hz dekodiert nichts.
- `jtty_update`: Zustand `growing`, `complete`, `expired` (etwa 8 s nach dem letzten
  Rahmen) oder `ended`; `start`/`latest` sind Stream-Sekunden, `gaps` die Stellen von
  `...`.
- `render` liefert `segment`, dann `pcm` (Base64, int16 LE) und zuletzt `rendered`, oder
  `rejected`.
- Bibliothekspfad: `LD_LIBRARY_PATH` schlägt die mitgelieferten Bibliotheken. Die Apps
  setzen die Variable für LoRa, darum entfernt `jtty_codec` sie.
- FFT-Pläne (`jt9_wisdom.dat` in `-a`) werden nur beim geordneten Ende gespeichert.
  Das Datenverzeichnis liegt deshalb dauerhaft unter `~/.cache/pluto-tx/jtty`.

## 3. Umfang in pluto-tx

Enthalten: ganze Nachrichten bis 80 Zeichen von Hand senden, Empfang mit mitwachsendem
Text, über Pluto, HackRF, Soundkarte und AIOC.

Nicht enthalten: Senden beim Tippen (`"final":false`), F1–F8-Vorlagen, Contest-Profile,
N1MM, automatische Antworten.

Bewusst abgewiesen statt stillschweigend geändert: Text über 80 Zeichen, mehrere Zeilen,
`%`-Platzhalter. Ersetzte Zeichen meldet die Oberfläche vor dem Senden.

## 4. Bekannte Grenzen des Originals

- Einzelne Fehldekodierungen sind auch bei WSJT-X möglich: In der Original-Beispielaufnahme
  erscheint bei −17 dB ein Rahmen `4>-P'`. Wir filtern das nicht nach.
- Die Empfindlichkeit ist vom Projekt noch nicht veröffentlicht.
