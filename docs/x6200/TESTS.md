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

**Leistung:** Das Gerät geht nicht unter 3/255 (rund 1,2 %); ein kleinerer Wert ändert nichts, gemessen mit 0, 1/255 und 2/255. Ganze Prozentwerte von 1 bis 20 kamen alle an. Nach dem Schreiben antwortet `rigctld` rund 1,6 s lang nicht, und einmal von fünf Mal lieferte das erste Lesen den alten Wert. Der Regler schreibt deshalb erst nach 0,25 s Ruhe im Hintergrund, und erst vor dem Tasten wird zurückgelesen. Der Regler beginnt bei 1,2 %, und Re-Arm fasst die Leistung nicht an. Vorher scheiterte Re-Arm nach E-STOP am Rücklesen.

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
| 22:43 | 14,092 MHz USB-D | 5 % | RTTY, 45 Baud | am HackRF in Echtzeit teilweise; aus der IQ-Aufnahme fehlerfrei: `TEST DE DA2JH DA2JH X6200 RTTY TEST 73` |
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
| 23:35 | VGA 20 dB, Vorverstärker an, Web-TRX im Browser | 2 von 2, −12 dB |

## Web-TRX, Empfänger und Sender zugleich

Beide am selben X6200, gemeinsamer `rigctld`. Der Empfänger wurde 1,5 s nach dem Aussenden getrennt: keine Fehler, PTT danach aus.

## Dauerlauf und Ende des Akkus

- **Empfangsblock simuliert:** 45 Minuten Audio im Schnelldurchlauf, 13 von 13 Nachrichten, auch um Minute 26 bis 30.
- **X6200 empfängt 29 Minuten** (00:00–00:29 UTC, HackRF VGA 20 dB): Nach 5 Minuten 1 von 2 Nachrichten, nach 25 und 28 Minuten keine, Exit 0 ohne Absturz. Die Einkopplung liegt mit rund −14 dB nah an der Grenze.
- **Das Gerät antwortete um 00:32 UTC nicht mehr auf CI-V**, der USB-Teil blieb sichtbar. Vermutlich Akku leer oder automatisches Abschalten, Start bei 62 % um 22:14 UTC. Wann genau, ist unbekannt. Die Ausfälle um Minute 25 und 28 können daran liegen; der Langlauf-Befund aus `docs/jtty/TESTS.md` ist damit weder bestätigt noch widerlegt.
- Die Sendeversuche danach wurden sauber abgewiesen, vor jedem Tasten. Es lief keine Sendung, als das Gerät ausfiel. Der Lauf mit starkem Signal (X6200 sendet, HackRF empfängt 30 Minuten) brachte deshalb kein Ergebnis.

## RADE aus dem Browser, Empfang mit HackRF im selben Web-TRX

Gemeldet am 2026-11-02: Aus `pluto-tx` gesendet dekodiert der HackRF in Web-TRX, aus demselben Browser gesendet nicht, obwohl der Wasserfall ein Signal zeigt. Das Signal war in Ordnung, der Empfänger verlor Daten: Solange ein Sender im selben Prozess läuft, auch im Leerlauf und auch in SSB, meldete der HackRF bei 4 MS/s rund 15 `O` je Sekunde.

Gemessen an 28,300 MHz, rund 0,1 W, Empfang HackRF LNA 24 dB, VGA 12 dB, Zoom 64, je 30 s (Rahmen mit Synchronisation):

| Sender | Empfänger | Vorher | Mit Korrektur |
|---|---|---|---|
| CLI, eigener Prozess | CLI, eigener Prozess | 248, 6 `O` | nicht betroffen |
| Web-TRX-Backend | CLI, eigener Prozess | 256, 6 `O` | nicht betroffen |
| CLI, eigener Prozess | Web-TRX (40 s) | 216, 6 `O` | nicht betroffen |
| Web-TRX-Backend | Web-TRX, selber Prozess | 8, 500 `O`, 0 % | 219–224, 6 `O`, 84–90 % |
| echter Browser, Datei im Loop (40 s) | Web-TRX, selber Prozess | kein Dekodieren | 328, 34 `O` |

Ursache ist der GIL: Der Web-TRX-Sendeweg rief seine Python-Blöcke im Leerlauf rund 2500-mal je Sekunde auf, jeweils mit rund 43 Werten, und die FFT für Zoom 64 lief dazu im Datenstrom des HackRF. Zoom 1 oder 2 MS/s beheben es ebenfalls. Bei einem Sender mit Soundkarte (X6200, AIOC) liefert die Browser-Quelle jetzt 40-ms-Blöcke und läuft der Karte 0,2 s voraus, denn nur mit größeren Blöcken lief die Karte 12-mal in 30 s leer. Pluto und HackRF als Sender behalten die alten Werte, bei ihnen ist das Problem also nicht behoben und auch nicht gemessen.

## FreeDV, Dauerlauf, Abbruch durch das Gerät

- **FreeDV 2020** über Funk, 28,092 MHz, 5 %, HackRF: 19 von 20 s synchron mit libcodec2 aus dem Empfangsaudio, Signal rund 16 dB über dem Rauschen, Spitze am Eingang des Geräts 0,5.
- **Dauerlauf:** HackRF empfängt 30 Minuten im eigenen Prozess (18:37–19:07, 28,092 MHz, 5 %). 5 von 5 Nachrichten im Empfangsfenster vollständig, bis Minute 27, +5 bis +9 dB, kein Absturz, 6 `O`. Die sechste fiel hinter das Ende des Fensters. Dazu eine erfundene Nachricht „]K?.O“ bei −17 dB, die der Codec als fertig meldet; das ist der bekannte Effekt aus `docs/jtty/TESTS.md`. Der Ausfall nach 26 Minuten trat hier nicht auf.
- **Abbruch durch das Gerät:** Das Gerät beendet manche Sendungen selbst, die PTT fällt ohne Meldung.
  - Von 28,28 bis 28,32 MHz bei 3 und 5 % mit RADE nach 0,7–1,8 s, das Meter zeigt SWR rund 2,2. Auf 28,092, 28,20, 28,40 und 14,236 MHz bei 5 % nicht, ebenso wenig auf 28,30 MHz bei 1,2 %.
  - Bei Sprache auch anderswo unregelmäßig: 28,092 MHz nach 1,8 und 3,3 s, 14,236 MHz nach rund 3 s, SWR dabei 1 bis 1,24.
  - **Ohne die App nachgestellt:** nur `pw-play` und CAT-PTT, 14,236 MHz, 2 %: Sprache brach in 2 von 5 Läufen ab (3,5 und 6,9 s), Dauerton in 0 von 3.
  - Ohne Ton bleibt die PTT stehen, auch beim Schreiben von Leistung, Frequenz und Modus. Die Ursache im Gerät ist unbekannt: SWR-Schutz, gespeicherter Tuner-Wert oder Einstrahlung in den Aufbau sind Vermutungen.
  - pluto-tx zeigt es jetzt als `warning` im HW-Feld. Web-TRX meldet einen Fehler und beendet die Sendung. Die CLI bemerkt es nicht.

## Desktop-Apps am echten Gerät

Beide Fenster offscreen am echten X6200, mit dem echten Tasten über die Bedienelemente:

- **pluto-tx** (14,236 MHz, 2 %, Datei als Quelle): Verbinden, SSB und LSB-D, Regler wirkt ungetastet, E-STOP 196 ms, Re-Arm und erneutes Tasten, Frequenz vom Gerät übernommen und beim Tasten gehalten, SSB wählbar nach RTTY, keine Ausnahme. Das erste Tasten fiel in beiden Läufen ab, siehe oben, und die Warnung erschien.
- **pluto-advanced-rx** (ohne Senden): Verbinden, Frequenz, Wasserfall, USB und LSB schalten `PKTUSB`/`PKTLSB`, keine Ausnahme. Empfang eines echten Signals in diesem Fenster ist nicht geprüft, die Frequenz folgt dem Gerät dort nicht. Modi, die das Gerät nicht empfangen kann, sind jetzt grau.

## Offene Punkte

- **Akku-Ende während einer Sendung** ist nicht gemessen.

- **RTTY in Echtzeit am HackRF** ist nur teilweise lesbar, aus der Aufnahme derselben Sendung aber fehlerfrei; das Signal hat keine Aussetzer (Hüllkurve im 1-ms-Raster, 22 ms je Bit, 170 Hz Shift). Der X6200 sendet also sauber, die Fehler entstehen im Echtzeit-Empfang am HackRF. Dazu passen die dort gemessenen Sample-Verluste bei JTTY.
- **PSK31:** in Echtzeit am HackRF teilweise lesbar. Ursache offen; der Decoder braucht die Nachführung, die Prüfung aus der Aufnahme war deshalb nicht aussagekräftig.
- **Vorlauf bei RTTY, PSK31 und Waterfall Writer:** Vor dem Tasten laufen 1,3 s Stille, damit der Vorspann auf Sendung geht. Zuvor gingen rund 1,2 s verloren. Ein Lauf mit Vorlauf zeigte den Vorspann, aber Fehler am Textende; das ist einmal gemessen und nicht weiter untersucht.
- **Empfang:** LSB schaltet das Gerät auf `PKTLSB`, ein LSB-Signal über Funk ist nicht geprüft. FM ist gesperrt: Das NFM des Geräts gibt Sprache ab, die FM-Zweige erwarten aber den Eingang des Diskriminators. Eine Sendung stellt das Gerät auf den Sendemodus und nicht zurück.
- **Waterfall Writer und Web-TRX:** Der Empfang über den X6200 in Web-TRX ist nur mit JTTY geprüft.
- **Ausfall einer Abfrage:** Einmal, unter der Last der Testsuite, antwortete `rigctld` 1,5 s lang nicht auf `l RFPOWER`, vor dem Tasten. In 162 gezielten Abfragen danach nicht wieder (alle unter 55 ms). Der Client wartet jetzt länger und fragt einmal nach.
- **rigctld bleibt laufen:** Einmal gestartet, hält er den seriellen Port, bis man ihn beendet. Andere Programme nutzen ihn über Netz (Hamlib NET rigctl, Port 4532).
- **Hänger:** Beim allerersten `kill -9`-Test blieb die App über 150 s vor dem Tasten stehen, PTT aus. In den folgenden Läufen mit Stack-Dump nicht wieder aufgetreten, Ursache unbekannt.
