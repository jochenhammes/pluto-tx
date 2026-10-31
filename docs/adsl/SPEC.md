# ADS-L: Protokoll-Notizen für den Empfänger

**Maßgeblich:** EASA „ADS-L 4 SRD860" Issue 2 (1.12.2025), Download über [ED Decision 2022/024/R](https://www.easa.europa.eu/en/document-library/agency-decisions/ed-decision-2022024r).

**Referenz für Testvektoren:** [`pjalocha/ADS-L`](https://github.com/pjalocha/ADS-L) (CC0), Commit `07f24bd`. `tests/data/adsl/gen_vectors.cc` erzeugt daraus `vectors.jsonl`.

ADS-L ist **kein LoRa**. Die Sender nutzen zwar oft LoRa-Chips (SX1262, SX1276), aber im FSK-Modus.

## Kanäle

| Kanal | Frequenz | Modulation | Chiprate | Hub | Status hier |
|---|---|---|---|---|---|
| M-Band (MDR) | 868,2 / 868,4 MHz | 2-GFSK, BT 0,5, Manchester | 100 kchip/s | ±50 kHz | implementiert |
| O-Band LDR | 869,525 MHz | 2-GFSK, BT 1,0 | 38,4 kchip/s | ±12,5 kHz | offen |
| O-Band HDR | 869,525 MHz | GMSK, BT 0,5 | 200 kchip/s | ±50 kHz | offen |

Issue 2 erklärt die O-Band-Werte aus Issue 1 ausdrücklich für überholt.

## Abstimmung und Bandbreite

Beide M-Kanäle kommen aus einem Empfangsbereich um 868,85 MHz: So liegt keiner auf dem DC-Spike, und 869,525 MHz passt später dazu. Der entferntere Kanal liegt 650 kHz daneben. Dazu kommen 130 kHz für das Kanalfilter und Reserve zum Rand, daher mindestens 1,8 MS/s. Von dort an hing der Empfang in den Messungen nicht mehr von der Rate ab (gemessen bis 5 MS/s).

Warum nicht genau zwischen die Kanäle (868,3 MHz)? Dann läge der DC-Spike 100 kHz neben beiden Kanalmitten, also in beiden Kanalfiltern. Gemessen (synthetisch): Ein Träger an dieser Stelle lässt bei −10 dB zum Nutzsignal noch alles durch, ab gleicher Stärke praktisch nichts mehr; bei der gewählten Abstimmung stört selbst +20 dB nicht. Der Wasserfall zeigt trotzdem 868,3 MHz in der Mitte, denn das ist nur ein Ausschnitt der Anzeige (`FftProbe.set_view_offset`), der Empfang bleibt davon unberührt.

Benutzung und Empfangstipps: `README.md`, Abschnitt ADS-L; CLI-Ereignisse: `pluto_cli/README.md`.

## M-Band-Rahmen

```
Präambel        01 × 5..12 (Standard 8), dann 1001 1001     nicht Manchester
Sync            0x72 0x4B                                   Manchester ab hier
Länge           1 Byte: Anzahl folgender Bytes, ohne Signatur
Netzwerk-Header 1 Byte
ADS-L-Daten     5 Byte Header + Payload (3 + 4·N Byte), XXTEA-verwürfelt
Parität         CRC-24, MSB zuerst
Signatur        optional, 68 Byte, nicht Manchester; wird ignoriert
```

Manchester: `1` → `01`, `0` → `10`. Bits im Byte werden MSB zuerst gesendet, Ganzzahlen sind Little Endian. Die Bit-Offsets der Felder zählen vom niederwertigsten Bit des Bytes aus.

Ein Traffic-Rahmen hat die Länge `0x18` (1 + 20 + 3 Byte), auf der Luft 432 Chips ab dem Sync, also 4,3 ms.

## Geklärte Widersprüche

| Frage | Quellen | Entscheidung |
|---|---|---|
| XXTEA-Wörter | Issue 1: `num_data_words = 6`; Issue 2: Datenblock / 4; Referenz: 5 | Länge der Daten / 4, bei Traffic 5. Mit den Referenzvektoren geprüft. |
| „Keine Position" | Issue 1: `0xFFFFFF`; Issue 2: `0x800000` | Beide gelten als ungültig. `0xFFFFFF` wäre sonst rund 1 m neben dem Nullpunkt, also praktisch nie echt. |
| Polarität | `dump1090-gg`: „payload inverted"; Spezifikation: `1` → `01` | Die Referenz-Sync-Bytes `55 99 95 A6 9A 65` entsprechen der Spezifikation. „Inverted" dürfte eine Chip-Einstellung von SoftRF sein. Der Empfänger prüft trotzdem beide Polaritäten. |
| Exponential-Dekodierung | Spezifikation: `2^e·(2^N+b) − 2^N`; Referenz: Mitte des Schritts (z. B. +1 bei e=1) | Spezifikationsformel, denn alle Beispieltabellen passen dazu (120 m/s = `0xC4`). Abweichung zur Referenz: höchstens ein halber Schritt. |
| Höhen-Offset | Issue 1: Tabelle −316 m, Text 320 m; Issue 2 und Referenz: 320 m | 320 m |
| Grenzwerte Steigrate | Beispieltabelle Issue 2: +119 / −118 m/s | Bestätigt: `0x0FF` = 119,0, `0x1FE` = −118,0. |

## Noch nicht ausgewertet

- Status-, Uplink- und Remote-ID-Payload: Es werden nur Typ und Absender angezeigt.
- Reed-Solomon-Rahmen (Error-Control-Bit gesetzt): werden verworfen, weil sie ohne RS-Dekoder nicht prüfbar sind. Die Spezifikation empfiehlt sie erst ab 64 Byte, also vor allem für HDR-Uplinks.
- Digitale Signatur: wird ignoriert.

## Messwerte Demodulator (synthetisch)

`python3 -m tests.adsl_measure`: Numpy-Nachbau des Kanalpfads, kein Funk und noch nicht durch GNU Radio. SNR bezogen auf den 200-kHz-Kanal, Kanalfilter 110 kHz / 40 kHz Übergang.

| Größe | Ergebnis |
|---|---|
| Empfangsquote | 6 dB: 21 %, 8 dB: 89 %, 10 dB: 99 %, ab 12 dB: 100 % |
| Frequenzablage bei 14 dB | ±50 kHz: 100 %, −60 kHz: 90 %, +60 kHz: 77 % |
| Geräteraten 1,8–5 MS/s | jeweils 100 % bei 14 und 20 dB |
| Rauschen, 40 s | 0 Kandidaten, 0 falsche Pakete |
| CPU | rund 2 % eines Kerns je Kanal, unter Dauerlast 3,5 % (nur Detektor, ohne Filter) |
| Starkes Signal 200 kHz daneben | +20 dB: 95 %; +40 dB: 22 %, weil dann überlappende Pakete verloren gehen |
| Falscher Kanal bis 60 dB | keine Fehlzuordnung (nur synthetisch; über Funk siehe Echttest) |

Bekannte Grenze: Ein Dauerträger im Kanal in gleicher Stärke wie das Nutzsignal stört den Diskriminator vollständig; bei −10 dB ist er folgenlos. Das kann eigene Störlinien des Empfängers bei sehr schwachen Signalen betreffen.

## Echttest (31.10.2026, Empfänger in JO43)

**Testsendungen, vom Betreiber freigegeben:** Pluto+ bei −40 dB über Funk, kurze Distanz, 868,2/868,4 MHz abwechselnd. Acht Serien zu je 8 Paketen, zusammen etwa 0,35 s Sendezeit. Inhalt: Zufallsadresse (Privacy-Tabelle), „am Boden", Position bei 0°N 0°E, Modellflugzeug. Das Sendeskript ist nicht Teil des Repos. Im OGN-Feed um diese Position, in vier der acht Serien mitgelesen, tauchte kein ADS-L-Paket auf; einmal stand dort ein einzelnes FANET-Paket (`OGNFNT`), dessen Absender ich nicht geklärt habe.

| Weg | Empfänger | Einstellung | Pakete |
|---|---|---|---|
| CLI | Pluto+ | fest 30 dB, Puffer 262144 | 8/8 |
| CLI | Pluto+ | Vorgabe `slow_attack`, Puffer 262144 | 8/8 (zwei Serien) |
| CLI | RTL-SDR | fest 40 dB | 8/8 (zwei Serien) |
| CLI | RTL-SDR | fest 20 dB | 5/8 und 2/8 |
| **Web-TRX bis Browser-Karte** | RTL-SDR | 40 dB (ADS-L-Vorgabe) | **8/8**, Marker und Felder korrekt |
| Web-TRX bis Browser | RTL-SDR | 20 dB (vor der Korrektur) | 0/8, 9 CRC-Fehler |

**Verstärkung:** Das RTL-SDR startet in App und Web-TRX mit fest 20 dB. Das ist hier zu wenig: Eine Rohaufnahme zeigte keine Lücken und kein Clipping, alle Bursts wurden gefunden, lagen aber nur rund 10 dB über dem Rauschen und scheiterten am CRC. Deshalb setzen Desktop-App und Web-TRX beim Wechsel zu ADS-L eine unveränderte RTL-Verstärkung auf 40 dB und beim Verlassen zurück; eine selbst gewählte bleibt. Die CLI nutzt ohnehin 40 dB.

**Nicht getestet:** die echte AGC des RTL-SDR. Eine frühere Fassung dieses Dokuments nannte sie „Standard-AGC", tatsächlich setzt `--gain-mode slow_attack` beim RTL-SDR fest 40 dB.

**Falscher Kanal:** Bei starkem Signal auf kurze Distanz wurde dasselbe Paket auch auf dem Nachbarkanal dekodiert (dort −47,8 kHz Ablage, Korrelation 0,58 statt 0,91). Ein Filter über beide Kanäle lässt jedes Paket nur einmal durch. Es gewinnt das zuerst ausgewertete, die Spalte „MHz" kann in diesem Fall also den falschen Kanal zeigen (1 von 8 Paketen im Test). Inhalt und Position bleiben richtig, weil das CRC gilt.

**Dauer-Empfang:** 39 Minuten am späten Nachmittag (16:32 bis 17:11 Uhr) auf Pluto+ und RTL-SDR ergaben kein echtes ADS-L-Paket. **Echter Flugverkehr ist also noch nicht bestätigt**, der Positionsabgleich mit OGN aus dem Plan steht noch aus.

Die OGN-Prüfung während dieses Laufs galt versehentlich JO31 statt dem Empfängerstandort JO43 und sagt deshalb nichts über die Reichweite aus. Eine Stichprobe von 2 Minuten für 80 km um JO43 (17:46 Uhr) zeigte einen ADS-L-Sender: `PAWE5A900`, stehend bei 53°33′N 10°10′E. Das Präfix `PAW` deutet auf PilotAware hin, das laut Community-Übersicht das O-Band nutzt; ob es auch im M-Band sendet, ist nicht geprüft.

**HackRF:** nicht am echten Gerät getestet, nur der synthetische Pfad bei seinen Raten.

**CPU:** Der gesamte Flowgraph braucht auf dem Testrechner etwa 190 bis 220 % eines Kerns, der Großteil davon entfällt auf den Grundzweig ohne Digimode. Die beiden ADS-L-Kanäle kommen mit etwa 40 Prozentpunkten dazu.

**Pluto-Puffer:** In 20 s gab es mit dem Standardpuffer 32768 einen Überlauf, mit 262144 keinen.
