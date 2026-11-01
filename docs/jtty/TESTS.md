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

**Maßnahmen:**
- Vorkompensation im Sender (`--no-drift-comp` schaltet sie ab), mit Kaltstart nach 5 Minuten Pause. Über Funk nur mit warmem Pluto gesehen, siehe HackRF unten: Die 16 Rahmen blieben auf 23 cm innerhalb weniger Hz. Der Kaltstart mit Ausgleich ist nicht gemessen.
- Suchfenster mindestens ±500 Hz ab 1 GHz, automatisch getestet; 1000 bis 2000 Hz reichen aber nicht immer, siehe HackRF.

Auch mit dem HackRF als Sender, ohne Pluto, fiel die Frequenz am frisch gestarteten RTL-SDR um rund 0,7 Hz/s. Ein Teil der Drift stammt also vom RTL-SDR.

## HackRF One

Sender mit VGA 0 dB ohne Verstärker, Empfänger RTL-SDR. Empfänger HackRF gegen Pluto+ bei −40 dB. Je Band und Richtung 4 Sendungen (ein Rahmen zweimal, drei Rahmen, längere Nachricht), 18:21–18:41 UTC.

| Richtung | Band | Ergebnis | Signal bei |
|---|---|---|---|
| HackRF → RTL-SDR | 144,160 MHz | 4 von 4 | 1424 Hz |
| HackRF → RTL-SDR | 432,150 MHz | 4 von 4 | 1282–1268 Hz |
| HackRF → RTL-SDR | 1296,200 MHz, ohne Korrektur | 0 von 4 | knapp unter 1000 Hz, außerhalb des Fensters |
| HackRF → RTL-SDR | 1296,200 MHz, Empfänger auf 700 ± 200 Hz | 4 von 4 | 999–949 Hz |
| HackRF → RTL-SDR | 1296,200 MHz, Sender `--freq-correction-ppm -0.65` | 4 von 4 | 1778–1760 Hz |
| Pluto+ → HackRF, LNA 40, VGA 30 dB | 144,160 MHz | 4 von 4 | 1523 Hz |
| Pluto+ → HackRF, LNA 40, VGA 30 dB | 432,150 MHz | 4 von 4 | 1612–1615 Hz |
| Pluto+ → HackRF, LNA 40, VGA 30 dB | 1296,200 MHz | 4 von 4 | 1852–1857 Hz |

Mit dem festen LNA 16 / VGA 16 dB der CLI war der Empfang schwächer: auf 2 m nichts dekodiert, auf 70 cm ein Rahmen verloren oder ein Anfang abgeschnitten, auf 23 cm 4 von 4.

**Folgerungen:**
- Auf 23 cm können schon rund 0,65 ppm Quarzfehler aus dem Suchfenster führen. Abhilfe: Audio Hz im Empfänger auf das Signal im Wasserfall stellen oder die ppm-Korrektur setzen.
- Die CLI hatte beim HackRF keine einstellbaren Stufen und ignorierte `--gain` still. Inzwischen gibt es `--lna-gain`, `--vga-gain` und `--rf-amp`, und `--gain` wird dort abgewiesen.
- Mit diesen Optionen (LNA 40, VGA 30 dB) über die CLI: 2 m bei 4 MS/s 4 von 4, SNR −3 bis −9 dB; 70 cm bei 4 MS/s 2 von 4 vollständig, in den anderen beiden fehlten Rahmen; 70 cm bei 2 MS/s 4 von 4, SNR bis +16 dB. Die Rate von 4 MS/s, der Standard des HackRF, verliert auf diesem Rechner offenbar Samples; Überläufe wurden dabei nicht gezählt.

## Offene Punkte

- **Langlauf:** Mit einem seit 26 Minuten laufenden RTL-SDR kam die Serie um 17:47 UTC nicht an, und der CLI-Empfänger endete nach 1720 s mit Exit 134. Die Ursache ist unbekannt. Der Codec allein ist es nicht: 13 von 13 Nachrichten über 70 Minuten Audio im Schnelldurchlauf. Die Wiederholung wurde auf Wunsch des Betreibers abgebrochen.
- **Nicht getestet:** Web-TRX und Desktop-Apps mit echter Hardware, AIOC, Soundkarte an einem Funkgerät, QSOs mit anderen Stationen.

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
