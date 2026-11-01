# JTTY — Test- und Sendeprotokoll

Getestet am 01.11.2026 mit `wsjt-codec` aus WSJT-X `v3.3.0-beta5`.

## Gegenprobe mit dem Original

Die Programme stammen aus dem entpackten Paket derselben Version; installiert wurde nichts.

- **Unser Sender → `rjtty`:** 3 von 3 Nachrichten über den echten Flowgraph.
- **`sjtty` → unser Empfangsblock**, im Vergleich zu `rjtty`, je 6 Dateien, 4-Rahmen-Text, Fading 0,5 Hz, Laufzeit 1 ms:

| SNR | wir | `rjtty` |
|---|---|---|
| −6 dB | 6 | 6 |
| −10 dB | 3 | 3 |
| −12 dB und darunter | 0 | 0 |

Die Pegelnachführung vor dem Codec kostet also nichts. Gezählt wurde nur der vollständige Text.

## Sendungen über Funk

Alle mit Pluto+ (Tezuka) bei −40 dB Dämpfung, Rufzeichen DA2JH im Text, Empfang mit dem RTL-SDR daneben, ohne Verstärkerkette laut Betreiber. Stand ohne den Driftausgleich, der erst danach gebaut wurde.

| UTC | Frequenz (Dial) | Inhalt | Ergebnis |
|---|---|---|---|
| 16:43–16:44 | 432,150 MHz | 3 Nachrichten | 2 von 3; die erste nach langer Pause fehlte, Ursache ungeklärt |
| 16:46–16:47 | 432,150 MHz | 6 × ein Rahmen, 2 × drei Rahmen | 8 von 8, Frequenz fällt von 1523 auf 1475 Hz |
| 16:50–16:51 | 144,160 MHz | 3 × ein Rahmen, drei Rahmen, 16 Rahmen | 5 von 5, von 1491 auf 1472 Hz |
| 16:52–16:53 | 1296,200 MHz | wie oben, Pluto warm | 5 von 5, von 1521 auf 1417 Hz |
| 17:19–17:20 | 1296,200 MHz | wie oben, Pluto 25 min kalt | 5 von 5, von 1793 auf 1617 Hz |
| 17:47–17:48 | 1296,200 MHz | wie oben, Pluto kalt, RTL-SDR 26 min warm | **0 von 5, ungeklärt**, siehe unten |
| 17:51–17:53 | 1296,200 MHz | zwei Sendungen zur Rohaufnahme mit dem warmen RTL-SDR, nicht dekodiert | Signal bei rund 1150 Hz |

## Drift auf 23 cm

Gemessen wurde das Zusammenspiel von Pluto und RTL-SDR. Welcher Anteil von welchem Gerät stammt, ist nicht getrennt.

- **Kaltstart:** Das Signal startet 293 Hz zu hoch und fällt anfangs um rund 4,4 Hz/s. Das passt zum Driftmodell aus `FT8_DRIFT_MODEL`.
- **Warm:** etwa −1,5 Hz/s.
- **Warme Geräte:** Das Signal lag rund 350 Hz zu tief, also unter dem festen Suchkanal ab 1200 Hz.
- **Dekodierung:** Der Decoder hat die Drift innerhalb der Sendung vertragen, auch über 16 Rahmen. Bei 1793 Hz lag das Signal aber schon am Rand des Suchkanals.

**Maßnahmen, automatisch getestet, aber nicht mehr über Funk geprüft:**
- Vorkompensation im Sender (`--no-drift-comp` schaltet sie ab), mit Kaltstart nach 5 Minuten Pause.
- Suchfenster mindestens ±500 Hz ab 1 GHz.

## Offene Punkte

- **Langlauf:** Mit einem seit 26 Minuten laufenden RTL-SDR kam die Serie um 17:47 UTC nicht an, und der CLI-Empfänger endete nach 1720 s mit Exit 134. Die Ursache ist unbekannt. Der Codec allein ist es nicht: 13 von 13 Nachrichten über 70 Minuten Audio im Schnelldurchlauf. Die Wiederholung wurde auf Wunsch des Betreibers abgebrochen.
- **Nicht getestet:** Web-TRX und Desktop-Apps mit echter Hardware, HackRF, AIOC, Soundkarte an einem Funkgerät, das Debian-Paket mit JTTY, QSOs mit anderen Stationen.

## Soundkarte

Vom Lautsprecher zum Webcam-Mikrofon (C920) über `pluto-cli` und die PipeWire-Standardgeräte:

- 3 von 3 Nachrichten fehlerfrei, auch die mit 17 s.
- `plughw:` scheitert, solange PipeWire das Gerät hält; die Systemvorgabe geht.

## Echte Signale aus dem WebSDR

Mitschnitte öffentlicher KiwiSDR, gelesen mit einem eigenen kleinen Client.

- **FT8 live über den Schallweg:** 14,074 MHz vom KiwiSDR auf den Lautsprecher, am Mikrofon dekodiert: 166 Dekodierungen in 3 Minuten.
- **JTTY in echter Bandaufnahme:** unser Signal in eine 7,078-MHz-Aufnahme gemischt, über den Schallweg dekodiert: 4 von 4.
- **Echte JTTY-Station:** nicht belegt. In rund 45 Minuten auf 7,078 MHz gab es nur Einzelrahmen um −16 dB, teils eindeutig unsinnig (`H)-,$`); auch die rufzeichenartigen halte ich für Fehldekodierungen.
- **Fehldekodierungen:** etwa eine je 5 Minuten bei Suche in mehreren Fenstern; auch in rund 28 Minuten Stille auf 1296 MHz zwei. Bei ±50 Hz um 1500 Hz gab es auf 15 Minuten keine, bei ±500 Hz zwei.
