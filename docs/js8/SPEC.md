# JS8 — Protokoll-Spezifikation aus dem gepinnten JS8Call-Quelltext

Ergebnis von J0 aus `docs/JS8_PLAN.md`. Jede Angabe unten ist mit Datei und
Zeile im gepinnten Quelltext belegt. Was hier steht, gilt für die
Implementierung in pluto-tx. Weicht der Quelltext davon ab, gilt der
Quelltext, und die SPEC wird korrigiert.

## 0. Quelle

| | |
|---|---|
| Repository | https://github.com/JS8Call-improved/JS8Call-improved |
| Tag / Commit | **v2.5.2** / `f0f0d01b357c8eb8786aee687e8b5c787c787159` (25.01.2026) |
| Lizenz | GPLv3 (wie pluto-tx) |
| Ubuntu-Paket | `js8call 2.5.2+ds-1` (universe) = derselbe Stand; enthält nur die GUI `/usr/bin/JS8Call`, **kein** Decoder-Binary |

**Warum dieser Stand?** JS8Call-improved enthält die komplette Geschichte:
- das Original `js8call/js8call` bis v2.2.0 (2020),
- dann v2.3 … v2.5.2,
- außerdem v3.0.x.

Die Protokolltabellen sind in allen drei Ständen gleich. Geprüft am
29.09.2026 mit `git diff`:
- JSC-Wörterbuch: 2.2.0 = 2.5.2,
- Befehls-, Huffman- und Gruppentabellen: 2.2.0 = 2.5.2 = 3.0.3,
- Encoder und Modulator: 2.5.2 → 3.0.3 nur Kommentare bzw. Umbenennungen.

3.0 benennt in der Oberfläche „Turbo“ in „JS8 40“ um. v2.5.2 ist also mit
allem interoperabel, was auf den Bändern läuft. Dazu ist v2.5.2 genau der
Stand des Ubuntu-Pakets, sodass Quelltext und Paket-Binary übereinstimmen.

Die Zeilenangaben unten beziehen sich auf diesen Commit. Die Dateien liegen
im Klon unter `js8call/` (install-js8.sh), in J0 unter `~/js8-ref/src/`.

## 1. Physikalische Schicht

### 1.1 Speeds (Submodes)

Quelle: `JS8_Include/commons.h:22-47`, `JS8_Mode/JS8Submode.cpp:44-139`.
Die Nummern der Speeds stehen in `JS8_Main/varicode.h` (`SubmodeType`).

| Speed | Nr. | Samples/Symbol @12 kHz | Symboldauer | Tonabstand | Periode | Startverzögerung | Costas | RX-SNR-Schwelle | rxThreshold (Hz) |
|---|---|---|---|---|---|---|---|---|---|
| NORMAL | 0 | 1920 | 160 ms | 6,25 Hz | 15 s | 500 ms | ORIGINAL | −24 dB | 10 |
| FAST | 1 | 1200 | 100 ms | 10 Hz | 10 s | 200 ms | MODIFIED | −22 dB | 16 |
| TURBO | 2 | 600 | 50 ms | 20 Hz | 6 s | 100 ms | MODIFIED | −20 dB | 32 |
| SLOW | 4 | 3840 | 320 ms | 3,125 Hz | 30 s | 500 ms | MODIFIED | −28 dB | 10 |
| ULTRA | 8 | 384 | 32 ms | 31,25 Hz | 4 s | 100 ms | MODIFIED | −18 dB | 50 |

- ULTRA ist in JS8Call deaktiviert (`JS8_ENABLE_JS8I 0`, `commons.h:27`)
  und kommt auch in pluto-tx nicht vor.
- Die Formeln stehen in `JS8Submode.cpp:57-68`:
  - Tonabstand = 12000 / Samples pro Symbol,
  - Bandbreite = 8 × Tonabstand (NORMAL 50 Hz, FAST 80, TURBO 160,
    SLOW 25),
  - Datendauer = 79 × Symboldauer.
- `rxThreshold` ist die Frequenztoleranz, mit der der Empfänger Rahmen zu
  einer Nachricht zusammenfügt (siehe 3.3).

### 1.2 Modulation

Quelle: `JS8_Mode/Modulator.cpp` (`readData`, Zeilen 170–205).

**Die Arbeitsannahme „GFSK wie FT8“ ist falsch.** JS8 benutzt
phasenkontinuierliches **8-FSK ohne Gauß-Pulsformung**, wie FT8 in WSJT-X
1.9, von dem JS8Call abgeleitet ist.

- Die Audiofrequenz eines Symbols ist `f0 + ton × Tonabstand`, mit Ton 0…7
  (`Modulator.cpp:186`).
- Erzeugt wird bei 48 kHz: `FRAME_RATE`, `Modulator.cpp:19`; ein Symbol
  umfasst 4 × Samples/Symbol.
- Amplitude und Hüllkurve:
  - Die volle Amplitude liegt ab dem ersten Sample an, es gibt **keine
    Anstiegsrampe**.
  - Ab Symbol 78,983, also 0,017 Symbole vor dem Ende, wird die Amplitude
    je Sample mit 0,98 multipliziert: ein kurzes Ausklingen
    (`Modulator.cpp:177, 198`).
- Referenz in pluto-tx: `tools/js8ref/mkwav.py` portiert diese Schleife
  1:1.

**Folge für J1:** `ft8.gfsk_iq()` wird für JS8 nicht mit Gaußfilter
benutzt. JS8 braucht einen CPFSK-Pfad mit Rechteckpulsen, entweder als
Option von `gfsk_iq` (z. B. `bt=None`) oder als eigene Funktion in `js8.py`.
FT8 bleibt unverändert; der Prüfsummentest gilt weiter.

### 1.3 Sendezeitpunkt

Quelle: `Modulator.cpp:52-90`.

- Perioden liegen UTC-bündig:
  - `jetzt_ms % (Periode × 1000)`,
  - Slotbeginn bei Vielfachen der Periode ab Epoch,
  - also 15 s ab :00, :15, :30, :45.
- Erstes Symbol = Slotbeginn + Startverzögerung (Tabelle 1.1).
- Fällt der Start in die Startverzögerung, wird die restliche Stille
  gekürzt. Bei späterem Start werden Symbole abgeschnitten.
- Der Decoder meldet DT relativ zu dieser Startverzögerung:
  `xdt - Mode::ASTART` (`JS8.cpp:2531`). Ein Signal, das pünktlich
  bei Slot + 0,5 s (NORMAL) beginnt, hat also DT ≈ 0.

### 1.4 Rahmen → Bits → Töne

Quelle: `JS8_Mode/JS8.cpp:2766-2906` (`JS8::encode`, Port von `genjs8.f90`).

1. **Nutzlast:** 12 Zeichen aus dem 64er-Alphabet
   `0-9 A-Z a-z - +` (`JS8.cpp:842-845`), je Zeichen 6 Bit, MSB zuerst →
   72 Bit.
2. **Rahmenflags:** 3 Bit („i3bit“, `TransmissionType`) folgen als Bits
   72–74 (`JS8.cpp:2810`). Werte:
   - `JS8CallFirst=1`,
   - `JS8CallLast=2`,
   - `JS8CallData=4`,
   - kombinierbar.
3. **CRC12:**
   - `boost::augmented_crc<12, 0xC06>` über 11 Bytes (`JS8.cpp:887`).
   - Die 11 Bytes enthalten die 75 Bits, der Rest ist 0.
   - Das Ergebnis wird mit **42** (XOR) verknüpft.
   - Die CRC wird als Bits 75–86 angehängt, MSB zuerst.
   - Bit 87 (das 88. Bit des Byte-Arrays) bleibt 0 und wird nicht gesendet.
   - Das ist die CRC von FT8 v1 (WSJT-X 1.9), **nicht** CRC14.
4. **LDPC(174,87):**
   - 87 Paritätsbits aus der 87×87-Paritätsmatrix `parity`
     (`JS8.cpp:958-1042`): Hex-Zeilen, je Hexziffer 4 Bit MSB zuerst, das
     88. Bit jeder Zeile ungenutzt.
   - Paritätsbit i = Σ(parity[i][j] · msg[j]) mod 2.
   - Das Codewort in Decoder-Reihenfolge ist **[87 Parität | 87 Nachricht]**:
     `bpdecode174` kopiert `cw[M:]` als Nachricht (`JS8.cpp:772`).
   - Die Tanner-Graph-Tabellen `Mn` (174×3, `JS8.cpp:592`) und `Nm` (87 Checks
     mit 5–7 Bits, `JS8.cpp:634`) beschreiben denselben Code. Geprüft mit
     `tests/test_js8_tables.py`: H·[p|m] = 0.
5. **Symbole:** je 3 Bits → ein Ton 0…7, MSB zuerst. **Kein Graycode**,
   anders als bei FT8. Der Decoder bildet `llr[3j]` aus Tönen 4–7 gegen
   0–3 (`whitening_processor.h:167`).
6. **Anordnung der 79 Töne** (`JS8.cpp:2833-2848`):

   | Symbol | 0–6 | 7–35 | 36–42 | 43–71 | 72–78 |
   |---|---|---|---|---|---|
   | Inhalt | Costas A | 29 Paritätssymbole | Costas B | 29 Nachrichtensymbole | Costas C |

   Die Arbeitsannahme „wie FT8“ trifft also nicht zu: Die Parität steht
   **vor** den Daten.
7. **Costas-Arrays** (`JS8_Mode/JS8.h:24-36`):
   - ORIGINAL (NORMAL): `4 2 5 6 1 3 0` an allen drei Positionen. Das ist
     nicht das FT8-Array `3 1 4 0 6 5 2`.
   - MODIFIED (FAST/TURBO/SLOW):
     - A: `0 6 2 3 5 4 1`,
     - B: `1 5 0 2 3 6 4`,
     - C: `2 5 0 6 4 1 3`.

**Empfangsseite:** CRC-Prüfung und Auspacken der 12 Zeichen stehen in
`JS8.cpp:890-938`. Die Rahmenflags kommen aus `decoded[72..74]`
(`JS8.cpp:1565`).

## 2. Nachrichtenschicht

Quelle: `JS8_Main/varicode.cpp`, `JS8_Main/varicode.h`, `JS8_jsc/*`,
`JS8_Mode/decodedtext.cpp`. Alle Tabellen stehen in
`pluto_tx/js8_tables.py`, erzeugt von `tools/js8_extract_tables.py`.

### 2.1 Die 72 Nutzbits eines Rahmens

`pack72bits`/`unpack72bits` (`varicode.cpp:782-821`) übertragen 64 + 8 Bit
auf 12 Zeichen des `alphabet72`. Davon werden nur die ersten 64 Zeichen
benutzt, identisch mit dem PHY-Alphabet.

Die ersten 3 Bit sind der **Rahmentyp** (`FrameType`, `varicode.h`).
Er ist unabhängig von den 3 Rahmenflags der PHY.

| Typ | Bits | Aufbau (Bits) | Pack / Unpack |
|---|---|---|---|
| Heartbeat (HB/CQ) | `000` | [3 Typ][50 Rufzeichen][11 Extra-Hi] + [5 Extra-Lo][3 bits3] | `packHeartbeatMessage` `varicode.cpp:1376`, über `packCompoundFrame` `:1542` |
| Compound | `001` | wie Heartbeat; Extra = Grid oder `nusergrid + Kommando` | `packCompoundMessage` `:1455` |
| CompoundDirected | `010` | wie Compound | `:1487-1494` |
| Directed | `011` | [3][28 Von][28 An][5 Kmd] + [1 von/P][1 an/P][6 Zahl] | `packDirectedMessage` `:1620`, `unpackDirectedMessage` `:1731` |
| Data (Huffman) | `10…` | NORMAL: [1=1][1=0][70 Huffman + Pad] | `packHuffMessage` `:1777` |
| Data (JSC) | `11…` | NORMAL: [1=1][1=1][70 JSC + Pad] | `packCompressedMessage` `:1845` |
| Fast-Data (JSC) | — | FAST/TURBO/SLOW: 72 Bit JSC + Pad, gekennzeichnet durch das PHY-Flag `JS8CallData` | `packFastDataMessage` `:1966` |

**Padding** (`:1824-1833`): hinter den Nutzbits eine 0, danach Einsen bis
72 Bit. Beim Entpacken wird bis zur letzten 0 abgeschnitten.

**Datenrahmen je Speed** (`buildMessageFrames`, `varicode.cpp:2168-2176`):
- **NORMAL:** Huffman oder JSC, je nachdem, was mehr Zeichen fasst
  (`packDataMessage` `:1903`). PHY-Flag 000.
- **FAST, TURBO, SLOW:** **nur JSC**, PHY-Flag `JS8CallData`
  (`JS8_FAST_DATA_CAN_USE_HUFF 0`, `:1962`).
- Folge: Ohne JSC-Wörterbuch ist Freitext in diesen drei Speeds weder zu
  senden noch zu lesen. Siehe Abschnitt 5.

### 2.2 Felder

- **Rufzeichen, 28 Bit:** `packCallsign`, `varicode.cpp:973-1050`.
  - Aufbau: 37·36·10·27·27·27. `/P` wird zum Portable-Bit.
  - Sonderfälle für 3DA0 und 3X.
  - Gruppen/Platzhalter als `NBASECALL + n` (`basecalls`, `:233-300`):
    - `<....>` = +1,
    - `@ALLCALL` = +2,
    - … `@QRO` = +54.
- **Compound-Rufzeichen, 50 Bit:** `packAlphaNumeric50`, `:883-970`.
  - Aufbau: 11 Zeichen, `[39][38][38][2][38][38][38][2][38][38][38]`.
  - Die Stellen 3 und 7 sind jeweils Leerzeichen oder `/`.
- **Grid, 15 Bit:** `packGrid`, `:1159-1181`.
  - Formel: `((lon+180)/2)·180 + (lat+90)`.
  - `nmaxgrid = 32767` bedeutet leer.
  - Werte ab `nusergrid = 32410` sind Kommandos (`packCmd`, `:1198`).
- **Zahl / SNR:** `packNum`, `:1184`: Wert auf −30…31 begrenzt, dann +31.
  Formatiert wird mit `formatSNR`, `:487`, z. B. `+05` / `-12`.
- **Kommandos:** `directed_cmds`, `varicode.cpp:63-132`; Mengen `:135-150`.
  - 5-Bit-Codes 0…31:
    - `" SNR?"=0`,
    - `" ACK"=14`,
    - `" SNR"=25`,
    - `" HEARTBEAT SNR"=29`,
    - `" "=31` (Freitext),
    - HB/CQ intern = −1.
  - `buffered_cmds` (MSG, QUERY …) hängen an den Resttext eine Prüfsumme an
    (`:2278-2305`, `checksum_cmds`):
    - 16 Bit: CRC-16/KERMIT → 3 Zeichen `alphabet`, Basis 41 (`:497`),
    - 32 Bit: CRC-32/BZIP2 (`:515`).
  - **Achtung beim Portieren:** `QMap::key(wert)` liefert den *ersten
    Schlüssel in aufsteigender Qt-Sortierung*, nicht in Quelltext-Reihenfolge.
    Bei doppelt belegten Werten gewinnt deshalb:
    - `" SNR?"` vor `"?"`,
    - `" QUERY MSGS"` vor `" QUERY MSGS?"`,
    - `" "` vor `"  "`,
    - `" CQ"` vor `" HB"` und `" HEARTBEAT"`.
- **Huffman** (`hufftable`, `:177-223`): 44 Zeichen, präfixfrei.
  - Der Encoder nimmt bei gleichem Präfix das längste Symbol (`:573`).
  - Der Decoder läuft in QMap-Reihenfolge (`:612`).
- **CQ-Varianten** (`cqs`, `:302`):
  - 0 `CQ CQ CQ`,
  - 1 `CQ DX`,
  - 2 `CQ QRP`,
  - 3 `CQ CONTEST`,
  - 4 `CQ FIELD`,
  - 5 `CQ FD`,
  - 6 `CQ CQ`,
  - 7 `CQ`.

  Im Heartbeat-Rahmen trägt Bit 15 des Extra-Felds die Kennung „CQ statt
  HB“ (`:1411`).

### 2.3 JSC (Wörterbuch-Kompression)

Quelle: `JS8_jsc/jsc.cpp`, Tabellen `jsc_map.cpp`, `jsc_list.cpp`.

- **Größe:**
  - 2 × 262 144 Einträge (`map`, `list`) plus 103 Präfixe,
  - rund **1,65 MB Wörter** je Tabelle, 12,6 MB C-Quelltext,
  - `js8call/jsc.json` 10,5 MB.
  - Damit gilt Planentscheidung 4: **zur Laufzeit** aus `js8call/` laden,
    nicht einchecken.
- **Kodierung** (`jsc.cpp:31-50, 52-95`): (s,c)-dichte Kodierung mit b=4,
  s=7, c=9, Wortindex über `map`, Trenner-Bit für ein folgendes
  Leerzeichen. Die Präfixsuche läuft über `prefix` und `list`
  (`jsc.cpp:196-238`).
- **Eigenheiten, die 1:1 übernommen werden:**
  - `decompress` gibt `map[j].str` vollständig aus (`jsc.cpp:159`),
    `compress` rückt dagegen um `map[j].size` vor.
  - Einträge mit `size ≠ strlen`:
    - `{"@ALLCALL", 7, 81}`,
    - `{"ROSIDS", 1, 262143}`,
    - in `list` 42 Stück.
  - Latin-1-Zeichen `\xa1`… sind Einzelzeichen.
  - Der Extraktor wurde gegen die kompilierten Arrays geprüft
    (`js8ref dump-jsc`): **0 Abweichungen in 524 391 Einträgen.**

### 2.4 Text → Rahmen (`Varicode::buildMessageFrames`, `varicode.cpp:2037-2331`)

**Eingabe:** Die Sendebox von JS8Call wandelt jeden Text in Großbuchstaben
um und filtert ihn (`JS8_Main/TransmitTextEdit.cpp:127, 258-285`). Erhalten
bleiben nur:
- Latin-1 32–127,
- 0x10 und 0x1A,
- die erweiterten JSC-Einzelzeichen (`Varicode::extendedChars()`), darunter
  auch `\n`.

`buildMessageFrames` sieht deshalb nie Kleinbuchstaben. Mit Kleinbuchstaben
**hängt es in einer Endlosschleife**; das ist mit `js8ref` nachgeprüft.
pluto-tx übernimmt den Filter (`js8_message.normalize_text`). Der Port
bricht an dieser Stelle mit einer klaren Meldung ab, statt zu hängen.

Pro Zeile wiederholt, bis der Text aufgebraucht ist:

1. Das eigene Rufzeichen am Zeilenanfang wird entfernt (`MYCALL:` bzw.
   `MYCALL `).
2. Ist ein Rufzeichen ausgewählt und beginnt der Text nicht mit
   `@ALLCALL`, CQ, HB oder einem Rufzeichen, wird das ausgewählte
   Rufzeichen vorangestellt (`:2091-2122`).
3. In dieser Reihenfolge wird versucht (`:2184-2204`):
   1. Heartbeat/CQ (`heartbeat_re`),
   2. Compound (Text beginnt mit `` ` ``),
   3. Directed (`directed_re` = Rufzeichen + Kommando + Zahl),
   4. sonst Daten.

   Nach einem Directed- oder Datenrahmen folgen nur noch Datenrahmen.
4. **Sonderfälle:**
   - `forceIdentify`: Ein reiner Datentext bekommt `MYCALL: ` vorangestellt
     (`:2155-2164`).
   - Compound-Absender oder -Empfänger: Zuerst kommt ein Compound-Rahmen
     `` `MYCALL GRID ``, dann ein CompoundDirected-Rahmen
     (`:2246-2268`).
5. **Rahmenflags:**
   - Der erste Rahmen der Zeile bekommt `JS8CallFirst`, der letzte
     `JS8CallLast` (`:2322-2325`).
   - Datenrahmen der schnellen Speeds tragen `JS8CallData`.
   - Die GUI korrigiert beim Senden:
     - First nur auf dem ersten gesendeten Rahmen,
     - Last immer auf dem letzten
     (`JS8_UI/mainwindow.cpp:6199-6209`).

**Was die GUI für die Standardaktionen erzeugt** (`mainwindow.cpp`):
- **CQ:** Text `CQ CQ CQ <GRID4>` (`:7084`). Das ergibt einen
  Heartbeat-Rahmen; angezeigt wird `DA2JH: @ALLCALL CQ CQ CQ JO31`.
- **Heartbeat:** Text `<CALL>: HEARTBEAT <GRID4>` (`:6982`). Angezeigt wird
  `DA2JH: @HB HEARTBEAT JO31`. Offset im Heartbeat-Unterband
  500–1000 Hz, `findFreeFreqOffset(500, 1000, 50)` (`:6990`).

Die Anzeige beim Empfänger baut `DecodedText` (`JS8_Mode/decodedtext.cpp`)
aus Rahmen und Flags. Seine Ausgabe steht in den Referenzvektoren
(`message`).

## 3. Übertragung und Empfang

### 3.1 Mehrteilige Nachrichten

- Nach jedem Rahmen ruft `stopTx()` wieder `prepareNextMessageFrame()` auf.
  Auto-TX bleibt an, der nächste Rahmen geht also in der **direkt
  folgenden Periode** raus (`mainwindow.cpp:5475-5490`).
- Die Arbeitsannahme „aufeinanderfolgende Slots“ ist damit bestätigt.

### 3.2 Anruffrequenzen

- Die USB-Dial-Frequenzen stehen in `JS8_Main/FrequencyList.cpp:26-39`:

  | Band | Dial (MHz) |
  |---|---|
  | 160 m | 1,8435 |
  | 80 m | 3,578 |
  | 60 m | 5,363 |
  | 40 m | **7,078** |
  | 30 m | 10,130 |
  | 20 m | **14,078** |
  | 17 m | 18,104 |
  | 15 m | 21,078 |
  | 12 m | 24,922 |
  | 10 m | 28,078 |
  | 6 m | 50,318 |
  | 2 m | **144,178** |

- Heartbeats liegen im Audio bei 500–1000 Hz.

### 3.3 Zusammensetzen beim Empfänger

- Puffer je Frequenz-Offset (`m_messageBuffer`); ein Rahmen gehört dazu,
  wenn `|Δf| ≤ rxThreshold(speed)` (`mainwindow.cpp:4814-4831`,
  Tabelle 1.1).
- Ein Rahmen mit First öffnet den Puffer, einer mit Last schließt ihn
  (`:4572-4593`, `:9218-9251`).

## 4. Decoder: Entscheidung aus J0

- **Kein** fertiges Decoder-Binary: Das Ubuntu-Paket hat nur die GUI.
  Seit 2.5 ist der Decoder C++ (`JS8.cpp`) im GUI-Prozess; ein Fortran-`js8`
  wie `jt9` gibt es nicht mehr.
- **`tools/js8ref/js8ref`** (J0, in pluto-tx) linkt die *unveränderten*
  JS8Call-Quellen:
  - `JS8.cpp`,
  - `JS8Submode.cpp`,
  - `kalman.cpp`,
  - `varicode.cpp`,
  - `decodedtext.cpp`,
  - `jsc*.cpp`.

  Abhängigkeiten: Qt6 Core (ohne GUI), FFTW3f, Boost. Gebaut wird mit
  `tools/js8ref/build.sh` in rund 15 s.

  `js8ref decode FILE.wav MASK` dekodiert einen UTC-bündigen
  12-kHz-Slot offline:

  | Messung | Ergebnis |
  |---|---|
  | Rufformat | JSON-Zeilen mit snr, dt, freq, frame, bits, message |
  | Laufzeit | **≈ 2,5 s** je Aufruf (i7, alle vier Speeds zusammen ≈ 2,6 s), überwiegend Prozessstart und FFTW-Pläne |
  | eigene Referenz-WAVs | alle vier Speeds bitgenau dekodiert |
  | Rauschtest | Schwelle bei ≈ −25 dB (Decoder-Skala; laut Tabelle NORMAL −24 dB) |

- **Vorschlag:**
  - Backend **`js8`** = `js8ref decode` als Subprozess je Slot, primär wie
    `jt9` bei FT8.
  - Der eigene numpy-Decoder („own“) bleibt Fallback und Gegenprobe, wie
    im Plan.
  - Für TURBO (6-s-Slots) reichen 2,5 s. Wird es knapp, kann ein
    dauerhafter `js8ref`-Prozess (stdin-Protokoll) die Startkosten sparen.

## 5. Abweichungen von den Arbeitsannahmen des Plans

| # | Plan | Quelltext | Folge |
|---|---|---|---|
| 1 | 8-GFSK, Gauß-Pulsformung | **CPFSK ohne Gauß**; volle Amplitude ab Symbol 0; nur ein Ausklingen von 0,017 Symbolen am Ende | J1: CPFSK-Synthese, nicht `gfsk_iq` mit BT |
| 2 | Bitreihenfolge „wie FT8“, Graycode | **kein Graycode**; Parität vor Daten; Costas-Arrays anders als FT8 | J1 `js8_phy` nach 1.4 |
| 3 | Referenz-Decoder `js8` evtl. vorhanden | existiert nicht; ersetzt durch `tools/js8ref` aus den Originalquellen | install-js8.sh baut `js8ref` (braucht `qt6-base-dev`, `libfftw3-dev`, `libboost-dev`) |
| 4 | Fehlt JSC → Senden mit Huffman | gilt **nur für NORMAL**; FAST, TURBO und SLOW senden und lesen Freitext nur mit JSC | JSC ist für Freitext außerhalb von NORMAL Pflicht; install-js8.sh erzeugt `js8call/jsc.json` |
| 5 | TX-Referenz-WAVs von der JS8Call-GUI über einen Loopback | Die Töne stammen aus JS8Calls eigenem `buildMessageFrames` + `JS8::encode` (bitgenau); das Audio aus einem 1:1-Port von `Modulator::readData`. Ein GUI-Mitschnitt wurde **nicht** gemacht (siehe Bericht) | GUI-Mitschnitt optional nachholen |
| 6 | 30 min Off-Air-Mitschnitte auf 7,078 / 14,078 MHz | Versucht, 0 Decodes; am Radio-Server ist keine geeignete KW-Antenne angeschlossen | entfällt vorerst; Gegenprobe mit echten Signalen später (anderer Standort bzw. Antenne) |
| 7 | Speeds, Periode, Startverzögerung, LDPC(174,87)+CRC12, 72+3 Bit, Rahmentypen, aufeinanderfolgende Slots, 7,078/14,078 MHz | **bestätigt** | — |

## 6. Referenzmaterial in pluto-tx

- `tests/data/js8/cases.tsv` → `vectors.jsonl`: 176 Fälle.
  - Das sind 44 Fälle × 4 Speeds, u. a.:
    - CQ-Varianten und Heartbeat, auch ohne Grid,
    - `@ALLCALL` mit 60 Zeichen und ein langer Text,
    - gerichtete Nachrichten und Kommandos (`SNR?`, SNR ±, `HEARTBEAT SNR`,
      ACK, `GRID?`, `GRID`, `INFO`, `STATUS?`, `QUERY MSGS`, `AGN?`,
      `DIT DIT`, `73`, `?`, Relay `>`, `MSG` mit Prüfsumme),
    - echte Compound-Rufzeichen (`/MM`, `EA8/`, `/QRP`) als Absender
      und/oder Empfänger, Gruppen (`@JS8NET`, `@HB`),
    - Freitext mit und ohne Kennung, `forceData`, Satzzeichen, Umlaute,
      doppelte Leerzeichen.
  - Pro Fall festgehalten: Text → Rahmen → Flags → 79 Töne → Anzeige.
  - Erzeugt mit `js8ref vectors`.
  - Das Grid `JO31` und das Gegenrufzeichen `DL1ABC` sind nur
    Platzhalter für die Tests.
- `tests/data/js8/wav/*.wav`: 12 kHz/16 Bit, slotbündig, aus
  `tools/js8ref/mkwav.py`. `decodes.jsonl` enthält die Referenz-Decodes
  von `js8ref`.
- Off-Air-Mitschnitte: keine, siehe `docs/js8/TESTS.md` (keine KW-Antenne).

## 7. Automatik (J9): Antworten, Heartbeat, Relay, Inbox

Quelle: `JS8_UI/mainwindow.cpp`, `JS8_Main/TxLoop.cpp`, `JS8_Main/Inbox.cpp`,
`JS8_UI/Configuration.cpp`, jeweils am gepinnten Commit `f0f0d01b`.
Die Zeilennummern beziehen sich auf diesen Stand.

Die Compile-Schalter `JS8_HB_ACK_SNR_CONFIGURABLE`, `JS8_CUSTOMIZE_HB` und
`STORE_RELAY_MSGS_TO_INBOX` sind im Quelltext nirgends definiert, also aus.
Es gilt jeweils der `#else`- bzw. der ausgeschaltete Zweig.

### 7.1 Empfang → Befehle

**Heartbeat-Rahmen** (`mainwindow.cpp:4627-4683`):
- `isAlt` → Befehl `from=<Rufzeichen>`, `to="@ALLCALL"`, `cmd=" CQ"`.
- sonst → `to="@HB"`, `cmd=" HEARTBEAT"`.
- Das Grid kommt jeweils aus `extra`.

**Gerichtete Rahmen** (`:4701-4761`):
- Die Teile `from`, `to`, `cmd`, `extra` kommen aus `directedMessage()`.
- Gepuffert statt sofort verarbeitet wird ein Befehl, wenn
  `isCommandBuffered(cmd)` gilt und der LAST-Bit fehlt, oder wenn ein
  Rufzeichen `<....>` ist. Er öffnet dann einen Puffer auf seinem Offset.
- Sonst geht der Befehl sofort in die Befehlswarteschlange.

**Datenrahmen:**
- Sie kommen in den Puffer, der innerhalb ±`rxThreshold` des Offsets liegt
  (`:4583-4595`, `:4812-4840`).
- Ein FIRST-Rahmen löscht einen bestehenden Puffer (`:4569-4581`).

**Puffer schließen** (`processBufferedActivity`, `:9416-9505`):
- Liegt die letzte Aktivität mehr als 60 s zurück, gilt der letzte Rahmen als
  LAST. Nach mehr als 90 s wird der Puffer verworfen.
- Vollständig ist der Puffer mit dem LAST-Rahmen. Dann wird der Text
  zusammengesetzt und rechts gestutzt.
- Befehle mit Prüfsumme: `lstrip`, dann die letzten 6 Zeichen (32 Bit) bzw.
  3 Zeichen (16 Bit) als Prüfsumme, getrennt durch ein Leerzeichen.
- Nur gültige Befehle kommen in die Warteschlange.

### 7.2 Befehle verarbeiten (`processCommandActivity`, `:9510-10441`)

Die Prüfungen laufen in dieser Reihenfolge:
1. **Verwerfen:**
   - Befehle mit `<....>` (`:9541`),
   - nicht erlaubte Befehle (`isCommandAllowed`, `:9546`).
2. **Einordnen:**
   - `toMe`: `to` ist das eigene Rufzeichen oder dessen Basisrufzeichen
     (`:9551`).
   - `isAllCall`: `to` enthält `@ALLCALL` oder `@HB` (`:9066`).
   - `isGroupCall`: `to` ist in den eigenen Gruppen (`:9070`); Standard ist
     keine Gruppe.
3. **Gehörte Stationen:** Rufzeichen, SNR und Zeit werden immer
   festgehalten (`:9556-9569`).
4. **Abbruchgründe:**
   - `avoid_allcall` (Standard aus): @ALLCALL wird ignoriert, außer CQ und
     HB (`:9668`).
   - Nur Allcall, `toMe` oder Gruppe gehen weiter (`:9675`).
   - Whitelist/Blacklist (Standard leer, `:9775-9792`).
   - **Allcall-Sperre:** Wurde diesem Absender in den letzten 15 min schon
     auf ein Allcall geantwortet, gibt es keine Antwort (`:9797`). Eingetragen
     wird bei HB-ACK (`:10121`) und bei Allcall-Antworten (`:10427`).
   - **Idle-Watchdog ausgelöst** → keine Antwort (`:9805`).
5. **Relay-Pfad:** Bei Autoreply-Befehlen außer MSG/QUERY mit gesetztem
   Relay-Pfad tritt der Pfad an die Stelle des Absenders (`:9813`).
6. **Antworten**, jeweils nur ohne Allcall, wo nicht anders angegeben:

| Befehl | Antwort | Beleg |
|---|---|---|
| ` SNR?` | `<FROM> SNR <snr>` (`formatSNR`) | `:9824` |
| ` INFO?` | `<FROM> INFO <MyInfo>`, nur wenn MyInfo gesetzt | `:9831` |
| ` STATUS?` | `<FROM> STATUS <MyStatus>`, Standard `IDLE <MYIDLE> VERSION <MYVERSION>` | `:9843`, `Configuration.cpp:1980` |
| ` GRID?` | `<FROM> GRID <eigenes Grid>` | `:9855` |
| ` HEARING?` | `<FROM> HEARING` + bis zu 4 zuletzt gehörte Stationen (neueste zuerst, ohne den Fragenden) | `:9865-9898` |
| `>` Relay (Relay nicht aus) | siehe unten | `:9901-10012` |
| ` MSG TO:` (Relay nicht aus) | Nachricht als STORE für das Basisrufzeichen ablegen, `<Pfad oder FROM> ACK` | `:10015-10056` |
| ` AGN?` (auch nicht Gruppe) | die letzte eigene Aussendung erneut | `:10059` |
| ` HB`/` HEARTBEAT` | siehe 7.4 | `:10068-10125` |
| ` HEARTBEAT SNR`, ` CQ`, ` ACK`, ` CMD` | keine | `:10128-10193` |
| ` MSG` | in die eigene Inbox (UNREAD), `<Pfad oder FROM> ACK` | `:10140-10174` |
| ` QUERY` `MSG <id>` | gespeicherte Nachricht an den Anfragenden (bzw. letzten im Pfad): `<Pfad> MSG <Text> FROM <Absender> [NEXT MSG ID <n>]`, nach dem Senden DELIVERED | `:10196-10289` |
| ` QUERY MSGS` (Autoreply an) | `<Pfad> YES MSG ID <n>`, ohne Allcall sonst `<Pfad> NO` | `:10292-10328` |
| ` QUERY CALL` (Autoreply an) | `<Pfad> YES <snr> (<seit>)`, wenn gehört | `:10331-10380` |

**Relay `>`:**
- Beginnt der Text mit einem Rufzeichen und dahinter `>` oder Leerzeichen
  (und ist es keine Gruppe), wird weitergeleitet: `<Text, erstes Trennzeichen
  als '>'> *DE* <FROM>`.
- Sonst, wenn der Text nicht mit `ACK` beginnt:
  - Der Pfad wird gebildet aus FROM plus allen ` *DE* X`/` VIA X` im Text
    (`parseRelayPathCallsigns`, `:10663`).
  - Antwort `<Pfad> ACK`.
  - Beginnt der weitergeleitete Text selbst mit einem Autoreply-Befehl, wird
    dieser mit dem Pfad erneut verarbeitet.

7. **Senden oder nicht:**
   - Ohne Antwort passiert nichts.
   - Eine Allcall-Antwort gibt es nur mit Autoreply an (`:10400`).
   - Keine Antwort, solange Text im Sendefeld steht oder ein offener Puffer
     an uns läuft (`:10413-10423`).
   - Mit „Autoreply-Bestätigung“ (Standard an) wartet eine Rückfrage 90 s;
     sonst geht die Antwort direkt in die Sende-Warteschlange (Priorität
     Normal, Offset = eigener) (`:10435-10439`).

### 7.3 Sende-Warteschlange (`processTxQueue`, `:10727-10792`)

- Prioritäten: Low 10, Normal 100, High 1000 (`mainwindow.h:654`).
- Die Nachrichten laufen eine nach der anderen. Voraussetzungen: gültiger
  Offset, keine laufende Aussendung, leeres Sendefeld.
- Priorität ≤ Low sendet nur, wenn die letzte Aussendung mehr als 30 s
  zurückliegt.
- **Automatisch gesendet** wird bei Priorität ≥ High, bei Text mit
  ` HEARTBEAT `, ` HB ` oder ` ACK ` oder bei Autoreply an. Sonst steht die
  Antwort nur im Sendefeld, und der Betreiber entscheidet.
- Gesendet wird als ganz normale Nachricht, also mit denselben Rahmen wie
  manuell eingegebener Text.

### 7.4 Heartbeat

- **Text:** `<MYCALL>: HEARTBEAT <GRID4>` (`sendHB`, `:6968-7000`).
- **Offset:** Liegt der eigene Offset bei höchstens 1000 Hz, wird er
  genommen. Sonst ein freier Platz in 500–1000 Hz, 50-Hz-Raster, zufällig
  (`findFreeFreqOffset`, `:6271`). Frei heißt: in den letzten 30 s keine
  Aktivität innerhalb ±50 Hz (`:6245`).
- **Priorität:** Low + 1.
- **Wiederholung:** 10, 15, 30 oder 60 min, oder frei 1–1440 min
  (`:6891-6966`). Der nächste Termin ist jetzt + Intervall, aufgerundet auf
  die Periode der Geschwindigkeit (`TxLoop.cpp:181-205`).
- **Kein Heartbeat in Turbo** (`canCurrentModeSendHeartbeat`, `:6665`).
- **HB-ACK:** `<FROM> HEARTBEAT SNR <snr> [MSG ID <n>]` auf einem freien Platz
  in 500–1000 Hz, Priorität Low + 1 (`:7002-7029`, `#else`-Zweig). Nur mit
  HB-Modus + Autoreply + HB-ACK an. Nicht, wenn:
  - ein Nachrichtenpuffer offen ist,
  - HB-QSO-Pause (Standard an) gilt und ein Rufzeichen gewählt ist,
  - der Absender auf der HB-Blacklist steht.

### 7.5 Idle-Watchdog

- Standard: 60 min ohne Tastendruck oder Mausklick (`TxIdleWatchdog`,
  `Configuration.cpp:2194`). Der Zähler steigt minütlich (`:2171`) und wird
  bei Bedienung zurückgesetzt (`:3477`).
- **Wird er ausgelöst** (`:12291-12340`):
  - Aussendung stoppen,
  - Autoreply, HB- und CQ-Schleife aus,
  - Sende-Warteschlange leeren,
  - Hinweis an den Betreiber. Erst nach dessen Bestätigung werden die
    Schalter wiederhergestellt.

### 7.6 Inbox (`Inbox.cpp`)

- **SQLite-Tabellen:**
  - `inbox_v1(id, blob)`; `blob` ist JSON
    `{"type","value","params":{UTC,TO,FROM,PATH,TDRIFT,FREQ,DIAL,OFFSET,CMD,SNR,SUBMODE[,GRID][,EXTRA][,TEXT]}}`
    (`:27-47`, `Message.cpp:184`, `mainwindow.cpp:10522-10558`),
  - `inbox_group_recip_v1(msg_id, callsign)`.
- **Typen:**
  - UNREAD: an mich,
  - STORE: für andere abgelegt,
  - DELIVERED: abgeholt.
- **Nächste Nachricht für ein Rufzeichen:** STORE mit `TO` = Rufzeichen oder
  Basisrufzeichen und nicht leerem Text, kleinste ID zuerst (`:10560`).
  „Lookahead“ ist die nächste ID danach (`Inbox.cpp:290`).
- **Gruppennachrichten:** höchstens 48 h alt (`Inbox.cpp:530-560`).

### 7.7 Standardwerte in JS8Call (`Configuration.cpp`)

| Einstellung | Standard | Zeile |
|---|---|---|
| AutoreplyOnAtStartup | an | 2098 |
| AutoreplyConfirmation | an | 2100 |
| TxIdleWatchdog | 60 min | 2194 |
| RelayOFF | aus (Relay also an) | 2104 |
| HeartbeatQSOPause | an | 2102 |
| BeaconAnywhere | aus | 2101 |
| AvoidAllcall | aus | 2191 |
| CallsignAging | 0 (keine Alterung) | 1973 |
| MyInfo | leer | 1977 |
| HB-Modus / HB-ACK (Menü) | aus | `mainwindow.cpp:2486-2489` |

### 7.8 Umsetzung in pluto-tx: Abweichungen und zusätzliche Grenzen

Nach Plan-Abschnitt 0/J9 ergänzt pluto-tx eigene Sicherheitsregeln. Sie
stehen nicht in JS8Call, sind als solche gekennzeichnet und schränken nur ein:
- **Alles startet aus**, auch Autoreply. Das weicht von JS8Calls Standard
  „Autoreply an“ ab. Autoreply-Bestätigung ist wie bei JS8Call an.
- **Anwesenheit:** Es gilt der Idle-Watchdog wie in JS8Call. Als Bedienung
  zählen Aktionen im Browser. Ist kein Browser mehr verbunden, wird sofort
  alles abgeschaltet.
- **Harte Grenze für automatische Aussendungen** (Antworten, HB-ACKs,
  Heartbeats) gegen Endlosschleifen zwischen Automaten: höchstens 20 pro
  Stunde, gleitend gezählt.
- **Nach einem Watchdog** bleiben Autoreply und HB-Modus aus, bis der
  Betreiber sie wieder einschaltet. JS8Call stellt die Schalter nach der
  Bestätigung des Hinweises wieder her.
- Web-TRX kennt weder JS8Calls „Sendefeld nicht leer“ noch „Rufzeichen
  gewählt“. Die HB-QSO-Pause und die Sperre durch einen Entwurf greifen dort
  deshalb nicht.
- `<MYVERSION>` ist `PLUTO-TX`.
- Band, Leistungsdeckel, NOTAUS und die Abbruchregel gelten unverändert wie
  bei manuellem Senden.
- **Ort:** Automatik braucht Empfang und Senden im selben Prozess, also
  nur Web-TRX. Die Qt-Apps (RX und TX getrennt) und `pluto-cli` bleiben
  manuell.
