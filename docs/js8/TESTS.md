# JS8 — Test- und Sendeprotokoll

Jede Aussendung im Rahmen von `docs/JS8_PLAN.md` wird hier eingetragen, mit
UTC-Zeit, Gerät, Frequenz, Leistung bzw. Dämpfung, Inhalt und Ergebnis.
Die Grenzen dafür stehen in Plan-Abschnitt 0: nur Amateurfunkbänder, Pluto
≥ 40 dB Dämpfung bzw. HackRF ohne Verstärker, ≤ 0 dBm, Rufzeichen DA2JH,
Serien ≤ 20.

## Aussendungen

| UTC | Gerät | Frequenz (Dial + Offset) | Leistung / Dämpfung | Inhalt | Ergebnis |
|---|---|---|---|---|---|
| — | — | — | — | *J0: keine Aussendung* | — |

Alle J7-Aussendungen am 30.09.2026 mit denselben Einstellungen:
- **Gerät:** Pluto+ (Tezuka), `ip:169.254.10.209`.
- **Frequenz:** 144,178 MHz USB + 1500 Hz. Das Signal liegt bei
  144,17948–144,17955 MHz; Tonabstand × 8 ≤ 50 Hz, im 2-m-Schmalband-/
  Digitalbereich.
  - **Ausnahme: die mit „70 cm“ markierten Web-Aussendungen.** Mein
    Testskript hatte die TX-Frequenz nicht gesetzt, deshalb nahm der separate
    Test-Server seinen Standardwert: 432,150 MHz USB + 1500 Hz.
  - Belegt durch das TX-Log des Test-Servers.
  - Das liegt im 70-cm-Band im Schmalband-Allmode-Bereich (432,1–432,4 MHz).
    Die Bandprüfung des Flowgraphs hat es zugelassen; Dämpfung und Rufzeichen
    waren wie oben.
  - Der RTL-SDR (auf 2 m) hat diese Aussendungen nicht gehört. Die spätere
    Web-Wiederholung setzt die Frequenz ausdrücklich und prüft sie.
- **Leistung:** TX-Dämpfung 40 dB (`hardwaregain` −40 dB), also weit unter
  0 dBm.
  - Vor jeder Nachricht gesetzt und geprüft; während eines Rahmens per
    `iio_attr` zurückgelesen.
  - Nach jeder Nachricht: −89,75 dB, LO aus.
- **Antenne:** der RTL-SDR steht als Empfänger daneben, Funkstrecke.
- **Rufzeichen:** DA2JH in jedem Rahmen, als Absender.

| UTC | Weg | Inhalt | Rahmen | Ergebnis |
|---|---|---|---|---|
| 04:49:05 | CLI | H3a: CQ (1 Rahmen), Ctrl-C im Wartezustand | 0 | nichts getastet, sicherer Zustand |
| 04:49:30 | CLI | H3b: @ALLCALL „J7 ABORT TEST DE DA2JH PLUTO“ (3 Rahmen), Ctrl-C 2,9 s in Rahmen 1 | 1 (abgebrochen) | ≈ 3 s Burst, kein Rahmen 2, sicherer Zustand |
| 04:51:15 | Web, 70 cm | H3c: @ALLCALL „J7 ABORT TEST DE DA2JH PLUTO“, NOTAUS 2 s in Rahmen 1 | 1 (abgebrochen) | sicherer Zustand |
| 04:52:55 | Web, 70 cm | H3c wiederholt, nach dem EBUSY-Fix | 1 (abgebrochen) | sicherer Zustand, Neuverbinden klappt |
| 04:53:15 | Web, 70 cm | H3d: dasselbe, Browser 2 s in Rahmen 1 geschlossen | 1 (abgebrochen) | „no browser connected -- ending the transmission“, nichts neu getastet |
| 04:57:15 | TX-App | H3e: dasselbe, Fenster 2 s in Rahmen 1 geschlossen | 1 (abgebrochen) | im Rahmen −40,000 dB / LO an, danach −89,75 dB / LO aus |
| 04:58:45 | CLI | H2 NORMAL CQ | 1 | gesendet; Spätstart DT +1,3 s, beide Decoder |
| 04:59:15 | CLI | H2 NORMAL HB | 1 | beide Decoder, DT −0,02 s |
| 04:59:30 | CLI | H2 NORMAL @ALLCALL 60 Zeichen | 5 | Spätstart: DT +2,6 s; Rahmen 1 keiner, 2–5 nur „own“ |
| 05:00:45 | CLI | H2 NORMAL gerichtet an DA2JH/P | 2 | Spätstart: Rahmen 1 keiner, 2 nur „own“ |
| 05:01:30 | CLI | H2 NORMAL SNR? an DA2JH/P | 1 | beide Decoder, DT −0,02 s |
| 05:02:50 | CLI | H2 FAST CQ | 1 | beide, DT 0,0 s |
| 05:03:00 | CLI | H2 FAST HB | 1 | Spätstart: nur „js8“, DT +3,3 s |
| 05:03:20 | CLI | H2 FAST @ALLCALL 60 Zeichen | 5 | alle 5 von beiden, DT 0,0 s |
| 05:04:10 | CLI | H2 FAST gerichtet | 2 | Spätstart: nur „js8“, DT +2,7…3,3 s |
| 05:04:30 | CLI | H2 FAST SNR? | 1 | Spätstart: nur „js8“, DT +3,6 s |
| 05:05:48 | CLI | H2 TURBO CQ | 1 | beide, DT 0,0 s |
| 05:06:00 | CLI | H2 TURBO HB | 1 | beide, DT 0,0 s |
| 05:06:12 | CLI | H2 TURBO @ALLCALL 60 Zeichen | 5 | alle 5 von beiden, DT 0,0 s |
| 05:06:48 | CLI | H2 TURBO gerichtet | 2 | beide, DT 0,0 s |
| 05:07:06 | CLI | H2 TURBO SNR? | 1 | beide, DT 0,0 s |
| 05:08:30 | CLI | H2 SLOW CQ | 1 | keiner: Drift +6 Hz über den Rahmen (Überkompensation) |
| 05:09:00 | CLI | H2 SLOW HB | 1 | keiner (Spätstart + Drift) |
| 05:09:30 | CLI | H2 SLOW @ALLCALL 60 Zeichen | 5 | Spätstart: Rahmen 1–2 keiner, 3 „own“, 4–5 beide |
| 05:12:00 | CLI | H2 SLOW gerichtet | 2 | keiner (Spätstart + Drift) |
| 05:13:00 | CLI | H2 SLOW SNR? | 1 | keiner (Spätstart + Drift) |
| 05:22:00 | CLI | H2b SLOW CQ, Driftkompensation an | 1 | beide, DT −0,02 s, Drift +1,3 Hz |
| 05:23:00 | CLI | H2b SLOW CQ, Driftkompensation aus | 1 | beide, DT −0,04 s, Drift −4,3 Hz |
| 06:40:30 | TX-App | H2 NORMAL @ALLCALL 60 Zeichen | 1 von 5 | Rahmen 1 um 0,64 s zu früh enttastet (Qt-CoarseTimer) = Abbruch, Rahmen 2–5 nicht gesendet; sicherer Zustand |
| 06:51:30 | TX-App | H2 NORMAL @ALLCALL 60 Zeichen (nach dem Timer-Fix) | 5 | alle 5 von beiden, DT −0,02 s, Drift +1,8…2,9 Hz |
| 06:53:00 | TX-App | H2 SLOW CQ | 1 | beide, DT −0,02 s, Drift +2,4 Hz |
| 06:55:00 | Web, 70 cm | H2 FAST @ALLCALL 60 Zeichen | 5 | gesendet (Test-Server-Log), am 2-m-Empfänger nicht hörbar |
| 06:56:00 | Web, 70 cm | H2 SLOW CQ | 1 | gesendet, am 2-m-Empfänger nicht hörbar |
| 06:58:50 | Web | H2 FAST @ALLCALL 60 Zeichen (Frequenz gesetzt und geprüft) | 5 | alle 5 von beiden, DT 0,00 s, Drift +0,2…0,7 Hz |
| 07:00:00 | Web | H2 SLOW CQ | 1 | beide, DT −0,02 s, Drift +1,2 Hz |
| 07:01:45–07:05:00 | CLI | H2 NORMAL erneut (nach dem Spätstart-Fix): CQ, HB, @ALLCALL 60, gerichtet, SNR? | 10 | alle 10 von beiden, DT −0,02 s, Drift +0,9…3,0 Hz |
| 07:06:20–07:08:30 | CLI | H2 FAST erneut: dieselben 5 Nachrichten | 10 | alle 10 von beiden, DT 0,00 s, Drift +1,2…2,1 Hz |
| 07:10:00–07:16:30 | CLI | H2 SLOW erneut: dieselben 5 Nachrichten | 10 | DT −0,02 s; beide 6, nur „own“ 2, keiner 2 (CQ, gerichtet 1/2, SNR? — Drift +2,3…4 Hz) |
| 08:46:00–08:52:30 | CLI | H2 SLOW mit Drift-Sitzung über Prozessgrenzen (`6a5a8e3`): dieselben 5 Nachrichten | 10 | alle 10 von beiden, DT −0,02 s, Drift +0,5…2,1 Hz; Kompensation −0,377 (kalt) → −0,155 Hz/s |

## Decoder-Messung (J2, Software, 29.09.2026)

**Aufbau:**
- eigene Wellenform plus AWGN, SNR in 2500 Hz (WSJT-Definition),
- zufälliger Offset 200–2800 Hz, DT −1 … +2 s, Drift ±0,5 Hz über den
  Rahmen,
- 20 Slots je Punkt,
- Vergleich: eigener numpy-Decoder („own“) gegen JS8Calls Decoder
  (`tools/js8ref`, „js8“).

| Speed | Decoder | −10 | −12 | −14 | −16 | −18 | −20 | −22 | −24 dB |
|---|---|---|---|---|---|---|---|---|---|
| NORMAL | own | 100 | 100 | 100 | 100 | 80 | 0 | 0 | 0 |
| NORMAL | js8 | 100 | 100 | 100 | 100 | 100 | 25 | 0 | 0 |
| FAST | own | 100 | 100 | 100 | 85 | 15 | 0 | 0 | 0 |
| FAST | js8 | 100 | 100 | 90 | 70 | 25 | 0 | 0 | 0 |
| TURBO | own | 100 | 100 | 35 | 0 | 0 | 0 | 0 | 0 |
| TURBO | js8 | 100 | 90 | 35 | 0 | 0 | 0 | 0 | 0 |
| SLOW | own | 100 | 100 | 100 | 100 | 100 | 100 | 50 | 0 |
| SLOW | js8 | 100 | 100 | 100 | 100 | 100 | 95 | 55 | 0 |

- **Keine falschen Decodes** in allen 1440 Versuchen.
- **Rauschen:** 1000 reine Rauschslots (250 je Speed) ergaben 0 Decodes
  beim eigenen Decoder.
- **Zeitmessung:**
  - Der DT-Fehler liegt unter 0,1 s.
  - Laufzeit „own“: 0,1–0,6 s je Slot.
  - Laufzeit „js8“: ≈ 2,5 s je Slot.
- **SNR-Schätzung** des eigenen Decoders: auf ±0,3 dB kalibriert
  (Korrektur +1,8 dB).
- **Schwellen in `tests/test_js8_rx.py`** (≥ 95 % des eigenen Decoders):

  | Speed | Schwelle |
  |---|---|
  | NORMAL | −16 dB |
  | FAST | −14 dB |
  | TURBO | −12 dB |
  | SLOW | −20 dB |

  Die ersten Planannahmen (NORMAL −18 dB, SLOW −22 dB) schafft unter diesen
  Bedingungen auch JS8Calls eigener Decoder nicht zu 95 %.

## Senden im Flowgraph (J3, Software, 29.09.2026)

**Entscheidung zum Mehrrahmen-Timing** (Plan, Risiko „Mehrrahmen-Timing“):

Zwischen zwei Rahmen in aufeinanderfolgenden Perioden bleibt nur die Periode
minus 79 Symbole:

| Speed | Lücke zwischen den Rahmen |
|---|---|
| NORMAL | 2,36 s |
| FAST | 2,1 s |
| TURBO | 2,05 s |
| SLOW | 4,72 s |

Ein Quellentausch im laufenden Graphen braucht auf dem Pluto 0,5–2,5 s
(`FT8_KEY_EARLY_S` = 3 s). „Ein `Ft8TimedSource` je Rahmen“ passt deshalb
nicht zuverlässig.

Umgesetzt ist:
- **Eine** `Js8SequenceSource` je Nachricht (`pluto_tx/js8_source.py`).
  - Sie wird einmal vor dem ersten Rahmen eingewechselt, 3 s Vorlauf.
  - Sie legt Rahmen i samplegenau an seine Periodengrenze.
- **Jeder Rahmen wird trotzdem einzeln getastet** (`key_ptt()`/`unkey_ptt()`
  je Rahmen, Vorlauf 0,5 s). Dafür ist kein Graph-Lock nötig, das Neutasten
  des Pluto-LO dauert 5 ms.
- **Zwischen den Rahmen ist der HF-Pfad dunkel:** `tx_gain` 0, Dämpfung
  maximal, LO aus. Der Test prüft Nullen zwischen den Rahmen.
- **Abbruchregel:** Ein `unkey_ptt()` vor dem geplanten Rahmenende bricht
  die ganze Nachricht ab (`js8_cancel()`). Das gilt für NOTAUS der GUI, den
  Abbruch und `shutdown_safe()`. Danach wird aus dieser Nachricht nie wieder
  gesendet; ein neues Tasten braucht einen neuen Slotplan.
- **Bandprüfung** `js8_problem()`: Die ganze belegte Bandbreite (Träger +
  Offset … + 8 Töne) muss in *einem* Amateurband liegen. 40 m und 20 m
  wurden ergänzt.

Tests: `tests/test_js8_flowgraph.py` mit Fake-Gerät, jeweils TURBO:
- Startzeit ±0,06 s,
- Dauer 3,95 s,
- drei Rahmen im Abstand von genau 6,000 s, je einzeln getastet und
  dekodiert,
- Abbruch nach Rahmen 1,
- NOTAUS mitten im Rahmen,
- Soundcard-Gerät,
- Bandkanten.

## Hardwaretests (J7, 30.09.2026)

**Aufbau:**
- Pluto+ auf 2 m (144,178 MHz USB + 1500 Hz), 40 dB Dämpfung.
- Daneben ein RTL-SDR über Funk, aufgezeichnet mit
  `tools/js8_record_slots.py record`.
- Die Aufnahme wird in UTC-Slots geschnitten. Jeder gesendete Rahmen wird mit
  beiden Decodern gesucht: eigener Decoder („own“) und JS8Calls Decoder
  (`tools/js8ref`, „js8“).
- Drift: lineare Anpassung der gemessenen Tonfrequenz gegen die bekannten
  Töne, über den Rahmen.

Kein KW: H1 (HackRF auf 40/20 m) und H5 (Mitlesen echter KW-Stationen)
entfallen, weil keine KW-Antenne angeschlossen ist.

### H3 — Abbruchpfade (zuerst geprüft)

Alle bestanden, siehe Tabelle oben:
- Ctrl-C im Wartezustand und im Rahmen,
- Web-NOTAUS,
- Browser geschlossen,
- TX-App geschlossen.

Danach war der Pluto jedes Mal bei −89,75 dB mit LO aus. Kein Folgerahmen
wurde gesendet, nichts wurde neu getastet.

Dabei gefunden und behoben: Nach einem Web-NOTAUS schlug das Neuverbinden mit
`EBUSY` (−16) fehl.
- **Ursache:** Die Js8Series hielt über ihre key/unkey-Closures den
  gestoppten Flowgraph samt TX-Puffer am Leben (Referenzzyklus).
- **Fix:** `js8_series.py` gibt die Aktionen am Serienende frei.

### H2 — CLI-Serie, erster Durchlauf

**Befund 1: Spätstart verschiebt die ganze Nachricht.**
- `plan_frames()` erlaubte wie FT8 einen Start bis 1 s nach Periodenbeginn.
  Die CLI braucht zusätzlich ≈ 2,6 s für den Quellentausch.
- `Js8SequenceSource` holte diese Verspätung nie auf, weil das Gerät in
  Echtzeit verbraucht. Deshalb lagen *alle* Rahmen der Nachricht bei
  DT ≈ +2,6 s. Das liegt am Rand bzw. außerhalb des DT-Fensters: JS8Calls
  Decoder verlor diese Rahmen, „own“ las die meisten.
- **Behoben:**
  1. **Planung:** JS8 startet nie verspätet. Es gilt die erste Periode, deren
     Start mindestens `JS8_KEY_EARLY_S` (3 s) entfernt ist
     (`JS8_LATE_START_MAX_S = None`).
  2. **Quelle:** Kommt sie trotzdem mehr als 0,5 s zu spät zum ersten Sample
     (Swap-Überlauf), überspringt sie das Vergangene. Die übrigen Rahmen
     bleiben so auf ihrer Periode, und die Tastfenster, die der Wanduhr
     folgen, schneiden nichts ab.
  3. **Quelle:** Sie arbeitet in Blöcken von mindestens 10 ms statt oft
     1 Sample pro Aufruf.

**Befund 2: pünktlich gestartete Rahmen.**
- NORMAL, FAST und TURBO: alle pünktlich gestarteten Rahmen von beiden
  Decodern gelesen, DT −0,02 … 0,00 s.
- SLOW: der pünktliche CQ wurde nicht gelesen.

**Befund 3: Drift (H2b).** Gemessen ist die Drift am Empfänger, also Pluto
plus RTL-SDR.
- **In der CLI-Serie, Kompensation an:** Die Frequenz steigt über den Rahmen:
  - NORMAL +2…3,5 Hz,
  - FAST +1,5…2,3 Hz,
  - TURBO ≈ +1 Hz,
  - SLOW +6 Hz (Tonabstand 3,125 Hz, deshalb kein Decode).
- **H2b**, zwei SLOW-CQs direkt hintereinander:
  - Kompensation an: +1,3 Hz, der erste Rahmen nach 8 min Pause.
  - Kompensation aus: −4,3 Hz.
  - Beide wurden gelesen.
- **Deutung:** Die unkompensierte Drift des Pluto+ beträgt bei 144 MHz
  ≈ −1,2 ppb/s, wenn er warm ist, und ≈ −2,5 ppb/s beim ersten Rahmen nach
  langer Pause.
- **Folgerung:** Das FT8-Driftmodell (`FT8_DRIFT_MODEL`) nimmt die Aufwärmzeit
  ab *Flowgraph-Start*. Die CLI startet für jede Nachricht einen neuen
  Flowgraph, sagt also immer „kalt“ (−2,85 ppb/s) voraus und überkompensiert
  in einer Serie um ≈ 1,6 ppb/s.
  - In TX-App und Web-TRX läuft der Flowgraph durch, dort greift das Modell
    wie bei FT8.
  - FT8 bleibt unverändert.

### H2 — TX-App, Web und CLI nach den Fixes

**Befund 4: TX-App enttastete zu früh.**
- `QTimer.singleShot` nutzt standardmäßig einen CoarseTimer. Der darf bis zu
  5 % des Intervalls *zu früh* auslösen.
- Bei 14,7 s Haltezeit wurde Rahmen 1 um 0,64 s zu früh enttastet. Das ist
  nach der Abbruchregel ein Abbruch, deshalb fehlten Rahmen 2–5.
- **Behoben:** Alle JS8-Timer der TX-App sind jetzt `Qt.PreciseTimer`.
  Zusätzlich stellt sich ein zu früh ausgelöster Rahmenende-Timer für den
  Rest neu, statt zu enttasten. Test:
  `test_early_timer_does_not_cut_a_frame`.
- Danach wurde auf der Luft 12–29 ms *nach* dem Rahmenende enttastet.

**Ergebnis H2 (nach allen Fixes):**

| Weg | Speed | Rahmen | beide Decoder | DT | Drift über den Rahmen |
|---|---|---|---|---|---|
| CLI | NORMAL | 10 | 10 | −0,02 s | +0,9…3,0 Hz |
| CLI | FAST | 10 | 10 | 0,00 s | +1,2…2,1 Hz |
| CLI | TURBO (1. Lauf, pünktlich) | 11 | 11 | 0,00…+0,02 s | +0,9…1,3 Hz |
| CLI | SLOW (vor dem Drift-Fix) | 10 | 6 (+2 nur „own“) | −0,02 s | +2,3…4 Hz |
| CLI | SLOW (Drift-Sitzung über Läufe) | 10 | 10 | −0,02 s | +0,5…2,1 Hz |
| TX-App | NORMAL | 5 | 5 | −0,02 s | +1,8…2,9 Hz |
| TX-App | SLOW | 1 | 1 | −0,02 s | +2,4 Hz |
| Web | FAST | 5 | 5 | 0,00 s | +0,2…0,7 Hz |
| Web | SLOW | 1 | 1 | −0,02 s | +1,2 Hz |

**Planziele H2:**
- **„100 % dekodiert“ und „DT ±0,3 s“:** erfüllt für alle Geschwindigkeiten
  über alle drei Wege.
- SLOW über die CLI erst, seit die CLI den Driftzustand über Läufe hinweg
  fortsetzt (Entscheidung des Betreibers, Befund 5).
- **„Frequenzfehler < 1 Hz über den Rahmen“:** meist *nicht* erfüllt (0,2…4 Hz).
  - Gemessen wird Pluto plus RTL-SDR zusammen. Die Restdrift schwankt mit dem
    Wärmezustand des Pluto zwischen −0,5 und −2,5 ppb/s, die Vorkompensation
    trifft das nur auf ≈ ±1,5 ppb/s.
  - Für NORMAL/FAST/TURBO (Tonabstand 6,25–20 Hz) ist das unkritisch.
  - Für SLOW (3,125 Hz über 25 s) ist es grenzwertig.

**Befund 5, auf Entscheidung des Betreibers behoben:** Die CLI merkt sich pro
Gerät Sitzungsbeginn und Ende der letzten JS8-Aussendung
(`$XDG_STATE_HOME/pluto-tx/js8_drift.json`).
- Ein Lauf innerhalb von 5 min danach setzt die Sitzung fort
  (`PlutoTxFlowgraph.restore_drift_history()`, additiv).
- Längere Pausen starten wieder kalt.
- FT8 ist unverändert.
- **Ergebnis:** SLOW über die CLI 10/10 von beiden Decodern, Drift von
  +2,3…4 Hz auf +0,5…2,1 Hz gesunken.
- Die Restdrift wächst in einer Serie weiter leicht an. Das Modell ist auf
  1296/432 MHz angepasst; eine 2-m-Anpassung wäre ein eigener Schritt.

### H4 — Interoperabilität

JS8Calls eigener Decoder (`tools/js8ref`, gebaut aus v2.5.2 `f0f0d01b`) hat
die Aussendungen mitgelesen, siehe die Spalte „beide Decoder“.

Abweichung vom Plan: Es gibt keinen Test mit der JS8Call-GUI (WAV-Import bzw.
Live-Empfang) und keinen Test mit einer echten Gegenstation. Ohne KW-Antenne
gibt es keine Gegenstation, und die GUI verwendet denselben Decoder.

## Empfang (J0, nur Empfang)

- **29.09.2026, 18:44–19:47 UTC:** HackRF, nur Empfang, je 30 min auf
  7,078 MHz und 14,078 MHz USB mit `tools/js8_record_slots.py`.
  - Ergebnis: 0 JS8-Decodes mit `js8ref`.
  - Im Audio nur ortsfeste Störträger (≈ 488 Hz und Vielfache, 2050 Hz).
- **Abgebrochen:** Am Radio-Server ist keine geeignete KW-Antenne
  angeschlossen (Betreiber, 29.09.2026). Die Aufnahmen wurden gelöscht.
- Off-Air-Referenzmaterial für JS8 gibt es deshalb vorerst nicht. Die
  Referenz stützt sich auf:
  - die bitgenauen Vektoren aus den JS8Call-Quellen,
  - die daraus erzeugten WAVs (`tests/data/js8/`).
