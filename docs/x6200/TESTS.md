# Xiegu X6200 — Messungen und Testprotokoll

Gerät: Xiegu X6200 mit OpenX6X00, 50-Ω-Dummy-Load, Akku. Messempfänger: HackRF One mit Whip-Antenne, ohne Verbindung zum Gerät. Stand 01./02.11.2026.

## Phase 0: Inventur

**USB:** Am Kabel erscheinen eine C-Media-Soundkarte (`0d8c:0012`, 44,1 und 48 kHz, 16 Bit, Wiedergabe mono und stereo, Aufnahme mono) und ein zweifacher serieller Port (`1a86:55d2`, `/dev/ttyACM0` und `/dev/ttyACM1`). CI-V antwortet nur auf einem davon, hier `ttyACM1`, mit ID `A4`.

**Hamlib:** Das Ubuntu-Paket 4.6.5 kennt Modell 3091 (`rigctl -m 3091`). Frequenz, Modus inklusive `PKTUSB`, PTT und die Meter funktionieren. `L RFPOWER` meldet `RPRT -5`, obwohl der Wert ankommt: Die Firmware antwortet auf das Schreiben mit dem Wert statt mit `FB`. Das Gerät prüft deshalb durch Rücklesen.

**Welche Betriebsart sendet USB-Audio?** 1 kHz über die Soundkarte, 0,5 W, Ton innerhalb der Sendung an und aus:

| Modus | Ergebnis |
|---|---|
| USB-D | ja, Ton bei +1 kHz, 45 dB über dem Boden des HackRF |
| LSB-D | ja, Ton bei −1 kHz, 26 dB |
| USB, LSB | nein, Mikrofon ist die Quelle |
| AM, NFM | nein, reiner Träger |

Folge: SSB-Sprache und Digimodes über USB-D und LSB-D. FM, POCSAG und APRS gehen über dieses Kabel nicht.

**Pegel:** PipeWire setzt die Soundkarte auf 40 %, wegen der kubischen Kennlinie rund −24 dB. Bei 100 % meldet das Leistungsmeter ab −16 dBFS Ausgangsleistung, ALC bleibt bis 0 dBFS bei 0. Das Meter ist bei 0,5 W sehr grob. Das Gerät setzt die Wiedergabe auf 100 %.

**PTT:**
- Rücklesen läuft bis zu rund 200 ms nach, in beide Richtungen. Das Audio darf erst danach öffnen.
- DTR und RTS tasten nicht.
- **Die PTT überlebt das Ende des Host-Prozesses und das Schließen des Ports**, mindestens 45 s lang (länger nicht gemessen). Eine Sendezeitbegrenzung der Firmware wurde dabei nicht beobachtet.
- Roundtrip eines CI-V-Befehls: rund 50 ms.

**Empfang:**
- Die USB-Ausgabe folgt nicht der Lautstärke am Gerät (AF 0 bis 128 gleich).
- Bei 100 % Capture-Lautstärke übersteuert sie (Spitze 32768 in USB, USB-D und NFM). Mit 50 % (rund −18 dB) Spitze rund 13 000.
- In AM deutlich leiser als in SSB.

**Nicht gemessen:**
- Zeit von PTT bis HF: Meine Flankenauswertung war unbrauchbar. Der reale Lauf mit JTTY zeigt keinen Verlust am Anfang, aber keine Zahl.
- Das Ziehen des USB-Kabels während einer Sendung.

## Sicherheit am Gerät

| Test | Ergebnis |
|---|---|
| `kill -9` der App während der Sendung | 3 von 3: PTT danach aus (Wächter) |
| NOTAUS während der Sendung | `force_safe_state()` 98 ms, PTT danach aus |
| NOTAUS aus dem Browser | PTT danach aus |
| Zeitlimit des Wächters, 3 s | PTT nach 3,0 s aus, Meldung `released timeout` |

## Funk, X6200 sendet

Dummy-Load, Empfang am HackRF über die Abstrahlung. Rufzeichen DA2JH im Text.

| UTC | Frequenz | Leistung | Inhalt | Ergebnis |
|---|---|---|---|---|
| 22:27 | 14,090 MHz USB-D | 5 % | JTTY, 6 Rahmen und 1 Rahmen, CLI | 2 von 2, +10 dB, 1504 Hz |
| 22:42 | 14,092 MHz USB-D | 5 % | JS8, CQ | dekodiert, +9 dB, vollständig |
| 22:43 | 14,092 MHz USB-D | 5 % | RTTY, 45 Baud | teilweise: `TESTHDE DA2/'# DA2JH X6200 RTTY TEST 78` |
| 22:48 | 14,092 MHz USB-D | 5 % | FT8, CQ | dekodiert, +14 dB, DT 0,2 s |
| 22:49 | 14,092 MHz USB-D | 5 % | PSK31 | teilweise: `DE DA23 tqH X6,Te SK31 TEST 73` |
| 22:50 | 14,092 MHz USB-D | 5 % | SSB, Zweiton | +700/+1900 Hz 44 dB, Gegenseite rund 16 dB |
| 22:51 | 14,092 MHz LSB-D | 5 % | LSB, Zweiton | −700/−1900 Hz 42 dB, Gegenseite rund 10 dB |
| 22:52 | 14,092 MHz USB-D | 5 % | Waterfall Writer ab 400 Hz | Energie im Durchlassbereich, nicht optisch geprüft |
| 22:54 | 14,092 MHz USB-D | 5 % | RADE, Sprache | HackRF synchron 12,5 s, SNR bis 9,9 dB, 1 Hz Versatz |
| 22:57 | 14,092 MHz USB-D | 5 % | JTTY aus Web-TRX im Browser | dekodiert, +11 dB |

## Funk, X6200 empfängt

HackRF sendet über die Whip-Antenne bei VGA 0 bis 20 dB ohne Verstärker auf 14,093 MHz; das Signal koppelt nur schwach ein.

| UTC | HackRF | Ergebnis |
|---|---|---|
| 22:37 | VGA 0 und 10 dB | nicht dekodierbar |
| 22:37 | VGA 20 dB | Rohaudio: Anfang der Nachricht, −11 dB |
| 22:38 | VGA 20 dB, Vorverstärker an, volle CLI | 3 von 4 erkannt, 2 vollständig, −10 bis −12 dB |

## Offene Punkte

- **RTTY und PSK31** sind nur teilweise lesbar, obwohl FT8 und JTTY dort +10 bis +14 dB haben. Ursache offen.
- **Ausfall einer Abfrage:** Einmal, unter der Last der Testsuite, antwortete `rigctld` 1,5 s lang nicht auf `l RFPOWER`, vor dem Tasten. In 162 gezielten Abfragen danach nicht wieder (alle unter 55 ms). Der Client wartet jetzt länger und fragt einmal nach.
- **rigctld bleibt laufen:** Einmal gestartet, hält er den seriellen Port, bis man ihn beendet. Andere Programme nutzen ihn über Netz (Hamlib NET rigctl, Port 4532).
- **Hänger:** Beim allerersten `kill -9`-Test blieb die App über 150 s vor dem Tasten stehen, PTT aus. In den folgenden Läufen mit Stack-Dump nicht wieder aufgetreten, Ursache unbekannt.
