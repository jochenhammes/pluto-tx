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
| Mindestrate | 2,4 MS/s statt der zuerst geplanten 2 MS/s, siehe Messung. Der HackRF bleibt deshalb auf seinen 4 MS/s. |
| Datenrate | Der Empfänger fasst Nachrichten je Flugzeug zusammen und gibt höchstens einmal pro Sekunde aus, was sich geändert hat. |

## Messwerte Demodulator (synthetisch)

`python3 -m tests.adsb_measure`: kein Funk, Pulslage zufällig gegen den Abtasttakt. SNR ist die Pulsleistung zum Rauschen in der gesamten Abtastbandbreite.

| SNR | 2,0 MS/s | 2,048 | 2,4 | 2,5 | 3,2 | 4,0 | 5,0 |
|---|---|---|---|---|---|---|---|
| 10 dB | 16 % | 0 % | 3 % | 9 % | 50 % | 82 % | 95 % |
| 12 dB | 39 % | 0 % | 28 % | 47 % | 89 % | 98 % | 100 % |
| 15 dB | 60 % | 2 % | 80 % | 95 % | 100 % | 100 % | 100 % |
| 20 dB | 77 % | 9 % | 100 % | 100 % | 100 % | 100 % | 100 % |

Unter 2,4 MS/s fällt ein Puls zu oft auf zwei Samples, benachbarte Bits werden dann mehrdeutig; das ist kein Schwellenproblem, 2,048 MS/s scheitert auch bei 30 dB meist (47 %). Deshalb werden tiefere Raten verweigert.

| Größe | Ergebnis |
|---|---|
| Rauschen, 60 s | 0 falsche Nachrichten, bei 2,4 und 4 MS/s |
| CPU | rund 6,5 % eines Kerns je Rauschen, rund 10 % bei 500 Nachrichten/s (nur Detektor) |
| DC-Spike in Pulsstärke | ohne Abzug 0 %, mit Abzug 100 %; deshalb sitzt ein `dc_blocker` fest im Signalweg |
| Zwei Rahmen überlappend | Der zweite geht verloren. Der erste übersteht einen um 10 dB schwächeren Störer zu 95 %, bei gleicher Stärke gar nicht. |
| Browser, 100 Flugzeuge | keine lange Aufgabe in 30 s |

## Echttest (31.10.2026, Empfänger in JO43, nur Empfang)

Pluto+, 2,5 MS/s, die vorhandene Behelfsantenne (nicht für 1090 MHz ausgelegt). Vergleich mit dem öffentlichen OGN-Feed (`OGADSB`, 400 km um JO43, zur selben Zeit 129 bis 182 Flugzeuge).

| Lauf | Ergebnis |
|---|---|
| 100 s `pluto-cli` | 1 Flugzeug, 5 Nachrichten; Adresse `4BB84C` auch im OGN-Feed |
| Verstärkungsreihe manuell 40–73 dB und AGC, je 30 s | 0 Nachrichten, dazu passend wenige, schwache Pulse in einer Rohaufnahme |
| 40 s Web-TRX bis Browser-Karte | 1 Flugzeug `DLH9PT`, 23 Nachrichten, davon 4 korrigiert, 138 kt im Sinkflug, 0 Audio-Frames |
| 10 min `pluto-cli` | 2 Flugzeuge, beide auch im OGN-Feed: `DLH5CP` 35 Nachrichten, `4BCDE7` 6 |

**Abgleich:** Der eine zeitnahe Positionsvergleich mit OGN lag 60 m auseinander. Die Höhe wich um 434 ft ab, wir melden die barometrische Höhe wie gesendet (−75 ft auf einem Anflug), OGN gibt einen anderen Bezug aus. Ein einzelner Vergleich ist kein statistischer Nachweis, und ob die 434 ft allein am Bezug liegen, habe ich nicht geprüft.

**Einordnung:** Empfangen wurden nur tief anfliegende Flugzeuge im Raum Hamburg, von rund 150 gleichzeitig gemeldeten. Dass nur nahe Flugzeuge durchkommen, spricht für die Antenne als Grenze, belegt ist das aber erst mit einer passenden Antenne. Das spiegeln auch die wenigen, schwachen Pulse in der Rohaufnahme wider.

**RTL-SDR (31.10.2026, später Abend):** bei 40 dB ein echtes Flugzeug (`51119B`) mit 13 Nachrichten. Es stand nicht im OGN-Feed, ein Abgleich fehlt also; das ist nur ein kurzer Nachweis, kein Vergleich mit dem Pluto.

**Nicht getestet:** HackRF am echten Signal. Eine Übersteuerung des RTL-SDR durch nahe Sender ist ebenfalls ungeprüft.

