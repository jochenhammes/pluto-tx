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
| Eigene Bake | Unkomprimiert, Standard-Symbol `/>`. |
| Internet | Kein Igate und kein APRS-IS in der App. |
