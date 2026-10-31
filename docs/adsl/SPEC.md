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
