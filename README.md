# pluto-tx

Eigene FM/SSB(USB)-Sendesoftware für den ADALM-PLUTO (Pluto+, Tezuka-Firmware), gebaut mit GNU Radio, weil fertige Software (SDRangel) den TX-Zweig beim "Stop" nicht wirklich abschaltet — siehe [Sicherheitsdesign](#sicherheitsdesign).

Lizenzierter Betrieb: für den Einsatz mit einer gültigen Amateurfunklizenz (hier: DE Klasse A). Verantwortung für Frequenzwahl, Bandplan und Sendeleistung liegt beim Betreiber.

## Struktur

```
pluto_tx/
├── config.py      # Konstanten: Dämpfungsgrenzen, Samplerates, DE-Bandplan
├── safety.py       # PlutoSafety: rohes python3-libiio, unabhängig von gr-iio
├── flowgraph.py     # PlutoTxFlowgraph(gr.top_block): FM/SSB-Signalkette
├── gui.py             # PyQt5 GUI
├── app.py              # CLI-Einstieg (--gui für die GUI)
└── da2jh-test.wav        # Standard-Testaufnahme (Rufzeichen, gesprochen)
pluto_rx/
├── config.py      # RX-Konstanten (importiert Bandplan/URI-Default aus pluto_tx.config)
├── flowgraph.py     # PlutoRxFlowgraph(gr.top_block): AD9361-RX -> FM/SSB-Demod -> Audio + Wasserfall
├── gui.py             # PyQt5 GUI: Frequenz, Feintuning, Gain, NF-Gain, RX-Bandbreite/Zoom, Wasserfall
└── app.py              # CLI-Einstieg
pluto_tx_carrier.py         # einfaches iio_attr/iio_writedev Carrier-Test-Skript (Fallback/Referenz)
```

`pluto_rx` ist bewusst unabhängig von `pluto_tx`: der RX-Zweig kann nicht senden, braucht also keine der TX-Safety-Mechanismen (`safety.py`) und ist nur lose über die gemeinsamen Konstanten (Bandplan, Default-URI) gekoppelt.

## Start

```
python3 -m pluto_tx.app --freq 432150000 --gui
python3 -m pluto_rx.app --freq 432150000
```

TX ohne `--gui`: headless CLI-Test (fester Carrier für `--duration` Sekunden, oder `--interactive` für Enter-zum-Keyen).

## Sicherheitsdesign

Kernproblem, das diesen Eigenbau motiviert hat: der AD9361-Treiber (libiio) trennt Buffer-Streaming komplett von Dämpfung (`hardwaregain`) und LO-Zustand (`powerdown`) — keine SDR-Software, die wir getestet haben (SDRangel eingeschlossen), setzt diese beim Stoppen automatisch zurück. Deshalb: `PlutoSafety` (in `safety.py`) verwaltet TX-Dämpfung und LO-Powerdown komplett unabhängig von GNU Radio über rohes `python3-libiio`. `force_safe_state()` (Dämpfung auf Minimum + LO aus) ist die einzige Funktion, auf die jeder Shutdown-Pfad läuft: normales Programmende, Fenster schließen, SIGINT/SIGTERM, unbehandelte Exceptions. Die GUI zeigt zusätzlich alle 500ms den tatsächlichen Hardware-Zustand an (nicht nur den vermuteten App-Zustand) und hat einen NOTAUS-Button.

PTT schaltet nur die Dämpfung (schnell, kein LO-Relock), nicht den LO-Powerdown — der ist für App-Start/-Ende reserviert.

## Bekannte Einschränkungen

- Datei-Wechsel zur Laufzeit ("Datei wählen…") ist nur ein Platzhalter — `blocks.wavfile_source` hat in dieser GNU-Radio-Version (3.10.12) keine Laufzeit-Datei-Wechsel-API, bräuchte `tb.lock()`/Reconnect.
- Die Audiodatei loopt im Hintergrund unabhängig von PTT (nur per Mute gesteuert) — die Wiedergabeposition wird beim PTT-Druck nicht auf Anfang zurückgesetzt.
- **Der AD9361 teilt sich einen Takt zwischen RX und TX.** Läuft `pluto_rx` gleichzeitig mit `pluto_tx` (das übliche Testsetup) und ändert `pluto_rx` seine RX-Bandbreite, verstellt das nachweislich den tatsächlich von `pluto_tx` gesendeten Takt (nicht den RX-Empfang!) — gemessen als Tonhöhenverschiebung um exakt den Bandbreiten-Faktor. `pluto_rx` allein (ohne gleichzeitig laufendes `pluto_tx`) ist davon nicht betroffen — verifiziert mit einem echten, externen UKW-Sender als Referenz. Noch ungelöst, siehe ToDo unten.

`pluto_rx`'s SSB-Demodulator ist ein komplexes Bandpassfilter (`firdes.complex_band_pass`, 300-2700 Hz oberhalb der abgestimmten Frequenz) direkt auf dem RX-IQ-Signal plus `complex_to_real` — das genaue Spiegelbild von `pluto_tx`'s Hilbert-basiertem USB-Modulator. Die RX-Bandbreite ("Zoom"-Stufen, aktuell 1/2,5 MHz) bleibt hinter den Kulissen immer auf eine feste Zwischenfrequenz-Rate (`DEMOD_IF_RATE = 50 kHz`) heruntergefiltert, damit der Demod-/Resampler-Zweig unabhängig von der gewählten Zoomstufe gleich bleibt — ein Bandbreitenwechsel baut daher den kompletten Flowgraph neu auf (GNU-Radio-Filter können ihr Dezimationsverhältnis nicht zur Laufzeit ändern). 5/10 MHz sind bewusst nicht als Presets verfügbar: über die aktuelle `ip:plutoplus.local`-Verbindung (IIOD-Netzwerkprotokoll über das USB-Gadget-Interface, nicht der native USB-Backend) kommt der Durchsatz messbar nur auf ~4,7-4,9 MSa/s mit Buffer-Overruns, was sich als abgehacktes/tonhöhen-verzerrtes Audio bemerkbar macht — siehe den Netzwerk/USB-ToDo-Punkt unten.

## ToDo für nächstes Mal

- **Geteilter AD9361-Takt zwischen `pluto_rx` und `pluto_tx` beheben.** Ein erster Versuch (`pluto_tx.key_ptt()` ruft vor jedem Senden `pluto_sink.set_samplerate()` erneut auf, um den Takt "zurückzuholen") hat das Problem in der Praxis NICHT gelöst — vermutlich weil das Zurückholen des Takts durch `pluto_tx` genau umgekehrt die aktuell in `pluto_rx` eingestellte Bandbreite wieder verstellt, sobald als nächstes `pluto_rx` etwas mit dem Takt macht (z.B. beim nächsten `set_frequency`/Retune, oder generell weil beide Apps denselben Takt für sich beanspruchen). Der Fix wurde deshalb wieder entfernt (siehe Git-History). Nötig ist vermutlich eine echte Recherche zu unabhängigem RX/TX-Takt auf dem AD9361/Pluto+ (Tezuka-Firmware) — z.B. ob/wie sich RX- und TX-Sample-Clock über libiio wirklich unabhängig konfigurieren lassen (eigene BBPLL-Teiler pro Richtung), oder ob das auf dieser Firmware/diesem Board grundsätzlich nicht getrennt werden kann. Bis dahin: `pluto_rx`-Bandbreite nicht ändern, während `pluto_tx` aktiv sendet.
- **Pluto auch über Netzwerk ansprechbar machen, nicht nur USB** — und das in der GUI auswählbar machen (aktuell ist die Context-URI (`ip:plutoplus.local` vs. `usb:...`) nur ein CLI-Flag (`--uri`), kein GUI-Feld. Braucht ein Auswahlfeld/Dropdown in `gui.py`, inkl. Scan/Erkennung verfügbarer Contexts). Der native USB-Backend könnte außerdem den RX-Durchsatz über die aktuell gemessenen ~4,7-4,9 MSa/s hinaus verbessern und 5/10-MHz-RX-Bandbreiten wieder nutzbar machen.
- **GUI besser/cooler aussehen lassen** — aktuell rein funktional (Standard-Qt-Widgets). Eventuell Inspiration von anderer SDR-Software (SDR++, SDRangel) oder modernen Ham-Radio-Interfaces holen.
