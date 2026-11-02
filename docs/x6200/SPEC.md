# Xiegu X6200 — Anbindung

Der X6200 mit der OpenX6X00-Firmware hängt über ein USB-Kabel am Rechner und zeigt dort eine Soundkarte und zwei serielle Ports. pluto-tx sendet und empfängt über die Soundkarte und steuert das Gerät mit Hamlib. Messwerte: [`TESTS.md`](TESTS.md).

## Aufbau

| Baustein | Aufgabe |
|---|---|
| `pluto_tx/devices/x6200.py` | Sendegerät: Audio-Weg, Frequenz, Modus, Leistung, PTT |
| `pluto_advanced_rx/devices/x6200.py` | Empfangsgerät: Audio der Soundkarte, Abstimmung über CAT |
| `pluto_tx/rigctl.py` | Client für das Protokoll von `rigctld`, Start von `rigctld` |
| `pluto_tx/rig_guard.py` | Wächterprozess für die PTT |

Der Verbindungstext lautet `<rig>|<audio>`:
- `<rig>`: serieller Port oder `rigctld://host:port`, optional mit `@<Hamlib-Modell>`.
- `<audio>`: ALSA-Gerät oder `monitor:<PipeWire-Knoten>`. Solange PipeWire die Karte hält, geht nur Letzteres.
- Leer: Port und Soundkarte werden gesucht, die Karte über den gemeinsamen USB-Hub. Ohne Karte wird abgewiesen, nie die Systemvorgabe genommen; sonst wäre das Gerät getastet und der Ton käme aus den Lautsprechern.

## Betriebsarten

Das USB-Audio geht nur in USB-D und LSB-D auf Sendung. Darum:

| Modus | Weg |
|---|---|
| JTTY, FT8, JS8, RTTY, PSK31, Waterfall Writer, RADE, SSB (USB) | USB-D |
| SSB (LSB) | LSB-D |
| FM, POCSAG, APRS, AM | nicht möglich, wird abgewiesen; nur über den TCI-Server der Firmware denkbar |
| FreeDV 2020 | USB-D, Modem-Audio vor dem Hilbert-Schritt, nicht in Web-TRX |
| M17, Baseband, LoRa, File Broadcast | nicht möglich |

## Sicherheit

CAT-PTT hat keinen Totmann: Stirbt der Host, bleibt das Gerät getastet (gemessen). Deshalb:

1. **Reihenfolge beim Tasten:** Frequenz und Modus setzen und zurücklesen, Bandprüfung am zurückgelesenen Wert, Leistung prüfen, Wächter informieren, PTT setzen, warten bis die PTT zurückgelesen an ist. Erst danach öffnet der Flowgraph das Audio. Jede Abweisung kommt vor der HF.
2. **Wächter:** Ein eigener Prozess, verbunden über eine Pipe, ohne GNU Radio. Er löst die PTT nach 120 s und sobald die Pipe schließt, also auch bei `kill -9`. SIGTERM und SIGINT ignoriert er, damit die App zuerst aufräumt.
3. **NOTAUS:** `force_safe_state()` nutzt eine eigene Verbindung und wartet nicht auf laufende Anfragen. Klappt das nicht, setzt es das Limit des Wächters auf einen sehr kurzen Wert.
4. **rigctld bleibt laufen:** Er wird nie automatisch beendet, weil ein Wächter oder eine zweite App ihn noch brauchen kann.
4a. **Frequenz am Gerät:** pluto-tx und Web-TRX fragen sie alle 0,5 s ab und übernehmen eine Änderung, ohne sie zurückzuschreiben; beim Tasten gilt, was gerade am Gerät steht. Während der Sendung wird nichts übernommen. pluto-advanced-rx und die CLI folgen nicht. Beim Verbinden setzt die App weiterhin ihre eigene Frequenz, sofern sie im Bereich des Geräts liegt.
5. **Leistung:** Standardgrenze 10 %, freigeschaltet bis 100 %. Die Rücklesung entscheidet, zu hohe Leistung führt zur Abweisung.

**Grenze:** Fällt der Rechner aus oder geht das USB-Kabel ab, kann auch der Wächter nichts mehr senden. Eine Sendezeitbegrenzung der Firmware wurde in 45 s nicht beobachtet. Am Funkgerät hilft dann die PTT-Taste oder Aus- und Einschalten.

## Eigenheiten des Geräts

- Die Antwort auf das Schreiben der Leistung ist der Wert statt `FB`; Hamlib meldet `RPRT -5`, obwohl der Wert ankommt.
- Die PTT-Rückmeldung läuft bis zu 200 ms nach.
- PipeWire setzt die Wiedergabe auf 40 %, das sind rund −24 dB; das Gerät setzt sie beim Verbinden auf 100 %.
- Bei 100 % Capture-Lautstärke übersteuert der Empfang; das Empfangsgerät setzt 50 %.
