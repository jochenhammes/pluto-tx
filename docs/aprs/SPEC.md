# APRS: Protokoll-Notizen

**Quellen:** APRS Protocol Reference 1.0.1; `aprslib` 0.7.2 als unabhängige Gegenprobe (`tests/data/aprs/gen_vectors.py` erzeugt daraus `vectors.jsonl`); `multimon-ng` als unabhängiger Modem-Maßstab.

## Funk und Rahmen

FM ±3 kHz, AFSK Bell 202 (1200 Bd, Mark 1200 Hz, Space 2200 Hz), NRZI, HDLC mit Bitstuffing, LSB zuerst. AX.25-UI-Rahmen: Ziel, Quelle, höchstens 8 Digipeater, CTRL `0x03`, PID `0xF0`, Info, FCS CRC-16/X.25.

## Dekodiert

Position unkomprimiert, komprimiert und Mic-E; mit Zeitstempel, Kurs/Tempo, Höhe `/A=`; Wetter; Status; Objekt und Item; Nachricht, Ack, Reject, Bulletin; Telemetrie (nur erkannt); Third Party (innerer Teil). Unbekanntes bleibt Rohtext.

## Entscheidungen

| Frage | Entscheidung |
|---|---|
| Positions-Ambiguität (Leerzeichen) | Mitte des angegebenen Bereichs. |
| Fehlerhafte Pakete | `parse_info()` wirft nie, getestet mit zufälligen Bytes. |
| Kurs und Tempo `000/000` | Gilt als „keine Angabe“. |
| Eigene Nachricht | Höchstens 67 Zeichen, druckbares ASCII, ohne `\|`, `~`, `{`. |
| Eigene Bake | Unkomprimiert, Standard-Symbol `/-` (Wohnhaus), Position aus der Mitte des Locators. |
| Internet | Kein Igate und kein APRS-IS in der App. |

## Messwerte

Synthetisch, 100 Pakete je Punkt, gleiche Daten für beide Decoder (`tests/aprs_measure.py`):

| Bedingung | unser Demodulator | multimon-ng |
|---|---|---|
| SNR 0 / 2 / 4 dB | 9 / 91 / 100 | 1 / 54 / 97 |
| SNR ab 6 dB | 100 | 100 |
| Kippung +12 / −6 dB | 100 / 100 | 68 / 98 |
| Kippung −12 dB | 0 | 0 |
| Präambel 20 ms | 100 | 93 |
| Takt ±1–2 % | 99–100 | 0, Ursache nicht geklärt, daher nicht als Vorteil zu werten |

Rauschen 120 s: 0 falsche Pakete.

## Über Funk

Alles von DA2JH, Pluto+ zu RTL-SDR nebeneinander auf 432,150 MHz bei −40 dB, ohne Digipeater-Pfad. Auf 144,800 MHz wurde nicht gesendet.

| Lauf | Ergebnis |
|---|---|
| CLI sendet, CLI empfängt live (Endstand) | **5 von 5**, alle Felder wie gesendet |
| Gleiche Aussendungen, Rohaufnahme durch den Flowgraph, Kanalfilter 8000/3000 Hz | 3 von 5 |
| Dieselbe Aufnahme, Kanalfilter 7000/2000 Hz (übernommen) | 5 von 5; Ablage 3,5 kHz wird im synthetischen Test weiter dekodiert |
| Web-TRX sendet, CLI empfängt live | 0 von 5, siehe Rechnerrauschen |
| Web-TRX sendet, Rohaufnahme | einzeln mit 7-kHz-Filter 0 von 5, mit 5,5 kHz 1 von 5; das Mittel der drei gleichen Baken wird dekodiert und besteht die FCS |

Das Kanalfilter ist an nur fünf Paketen gewählt, das ist ein schmaler Beleg. Die Vergleichszahlen mit `multimon-ng` über Funk
stammen aus einer blockweisen Auswertung, die Pakete an den Blockgrenzen zerschneiden kann; sie taugen nicht als Vergleich.

**Empfang, 144,800 MHz, ohne Sendung, Antenne nicht für 2 m ausgelegt:** CLI und `multimon-ng` je 3 gleiche Pakete auf derselben
Aufnahme (120 s). Web-TRX 6 Pakete von 3 Stationen in 200 s, 2 auf der Karte. Die beiden Objekte von DB0WOT stehen mit gleicher
Position auch in APRS-IS; das Statuspaket von DB0ET fand sich dort nicht.

**Trägervorlauf (behoben):** Zuerst lief vor jedem Paket rund 3 s unmodulierter Träger mit voller Leistung, weil die Quelle erst
nach dem Tasten per `lock()`/`unlock()` eingesetzt wurde. Jetzt wird sie eingesetzt, solange alles dunkel ist, und startet ohne
Sperre (`armed_source.py`): rund 1,05 s je Sendung, Vorlauf unterhalb der 50-ms-Auflösung. POCSAG nutzt seitdem dieselbe Quelle,
dort aber nur mit Fake-Geräten geprüft, nicht über Funk.

**Rechnerrauschen:** Reine CPU-Last hebt den Pegel im Kanal am danebenstehenden RTL-SDR von −57 auf −41 dB, ganz ohne Pluto. Bei
verbundenem Web-TRX-Sender sind es dauerhaft −42 dB, obwohl der Pluto nachweislich dunkel ist (−89,75 dB, LO aus). Die Sendung liegt
bei −36 dB, also nur rund 7 dB darüber. Das ist eine Grenze dieses Aufbaus. Bei Betrieb mit getrennten Antennen ist die Wirkung nicht gemessen.

## Nicht getestet

- Senden auf 144,800 MHz und Empfang durch fremde Stationen oder Digipeater.
- HackRF, Soundkarte und AIOC über Funk; HackRF auch beim Empfang.
- Die Sendeknöpfe der Desktop-Apps am echten Gerät (nur Tests mit Fake-Gerät).
- „Automatisch bestätigen“ über Funk (nur Tests mit Fake-Gerät und Simulator).
- Komprimierte Positionen und Mic-E an echtem Verkehr im Vergleich mit APRS-IS; sie sind nur gegen `aprslib` geprüft.
