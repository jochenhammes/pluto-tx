# ADS-B: Protokoll-Notizen für den Empfänger

**Nur Empfang.** 1090 MHz ist ein sicherheitskritischer Luftfahrtkanal. Dieses Projekt sendet dort nicht, auch nicht zum Testen. Der Test-Encoder unter `tests/` erzeugt ausschließlich Daten im Speicher.

**Quellen:** J. Sun, „The 1090 Megahertz Riddle" (Beispiele in den Tests); `pyModeS` 2.22.0 als unabhängige Gegenprobe (`tests/data/adsb/gen_vectors.py` erzeugt daraus `vectors.jsonl`).

## Funk

- 1090 MHz, Puls-Positions-Modulation, 1 Mbit/s.
- Präambel 8 µs: Pulse von 0,5 µs bei 0, 1, 3,5 und 4,5 µs.
- Danach je Bit eine Mikrosekunde: Puls in der ersten Hälfte = 1, in der zweiten = 0.
- 112 Bit = 120 µs inklusive Präambel.

## Rahmen

`DF[5] CA[3] ICAO[24] ME[56] PI[24]`. Ausgewertet wird nur DF17 und DF18 mit CF 0/1. DF18 mit anderem CF ist weitergeleiteter Verkehr (TIS-B, ADS-R) und bleibt draußen. Die Parität nutzt dasselbe CRC-24 wie ADS-L (`pluto_tx/mode_s_crc.py`), bei DF17/18 ist der Rest 0.

| Type Code | Inhalt |
|---|---|
| 1–4 | Rufzeichen |
| 5–8 | Position am Boden, Geschwindigkeit, Kurs |
| 9–18 | Position, barometrische Höhe |
| 19 | Geschwindigkeit, Kurs oder Steuerkurs, Steigrate |
| 20–22 | Position, GNSS-Höhe |
| 28 Subtyp 1 | Notfall, Squawk |

## Entscheidungen

| Frage | Entscheidung |
|---|---|
| Gillham-Höhe (Q = 0) | Als unbekannt gemeldet. `pyModeS` dekodiert sie; bei echtem ADS-B ist sie praktisch nie im Gebrauch, und eine geratene Höhe ist schlechter als keine. |
| Position | Erst global aus einem Paar höchstens 10 s alter Nachrichten, danach lokal zum letzten Fix, solange der höchstens 60 s alt ist. |
| Boden-Position | Braucht eine Referenz: Stations-Locator oder ein letzter Fix. Ohne die bleibt sie leer. |
| Plausibilität | Ein Sprung über 0,7 km/s + 5 km verwirft den Fix. Mit bekannter Station werden Positionen weiter als 800 km entfernt verworfen. |
| Bitkorrektur | Nur ein Bit und nur für Adressen, die schon unkorrigiert gehört wurden. |
| Anzeige | Ein Flugzeug erscheint erst nach zwei gültigen Nachrichten. |
