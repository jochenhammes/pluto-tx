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
