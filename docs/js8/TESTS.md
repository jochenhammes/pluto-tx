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
