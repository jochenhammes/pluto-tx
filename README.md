# pluto-tx

Eigene FM/SSB(USB)-Sendesoftware für den ADALM-PLUTO (Pluto+, Tezuka-Firmware), gebaut mit GNU Radio, weil fertige Software (SDRangel) den TX-Zweig beim "Stop" nicht wirklich abschaltet — siehe [Sicherheitsdesign](#sicherheitsdesign).

Sendebetriebn nur mit einer gültigen Amateurfunklizenz möglich. Verantwortung für Frequenzwahl, Bandplan und Sendeleistung liegt beim Betreiber.

## Installation (auf einem anderen Rechner)

Repo auf den Zielrechner bringen (klonen oder kopieren), dann:

```
./install.sh
```

Das Skript ist für Debian/Ubuntu-artige Systeme (`apt-get`) gedacht und installiert alles, was die Apps tatsächlich brauchen:

- **`gnuradio`** — das Debian/Ubuntu-Paket zieht dabei automatisch `gr-iio` (die `iio`-Blöcke, z.B. `iio.fmcomms2_sink_fc32`/`fmcomms2_source_fc32`) und `python3-pyqt5` als harte Abhängigkeiten mit.
- **`python3-libiio`** — die *rohen* libiio-Python-Bindings (`import iio`), getrennt von `gnuradio.iio` oben. Wird direkt von `pluto_tx/safety.py` (TX-Dämpfung/LO-Powerdown, unabhängig von GNU Radio) und `pluto_tx/netutil.py` (Verbindungs-Timeout, Geräte-Scan) gebraucht — `gnuradio` allein bringt das NICHT mit.
- **`libiio-utils`** — `iio_info`/`iio_attr`, nicht zwingend nötig für die Apps selbst, aber praktisch zum manuellen Nachschauen auf der Kommandozeile.
- **`avahi-daemon`** — löst `*.local`-mDNS-Hostnamen wie `plutoplus.local` tatsächlich auf. Wichtig: `libiio0` zieht zwar automatisch die Avahi-*Client*-Bibliotheken mit (harte Abhängigkeit), der eigentliche Daemon ist aber nur ein apt-„Suggests" — ohne dieses Paket würde `plutoplus.local` auf einem frisch installierten Rechner NICHT auflösbar sein, nur eine nackte IP-Adresse. Das Skript aktiviert den Dienst danach auch gleich per `systemctl enable --now`.
- **`python3-pyqtgraph`** — für `pluto_advanced_rx`'s interaktives Wasserfall-Widget. Auch das ist nur ein apt-„Recommends" von `gnuradio`, kein hartes Dependency — deshalb explizit gelistet.
- **`git`** — zum Klonen/Updaten dieses Repos, falls noch nicht vorhanden.

Danach prüft das Skript per echtem Python-Import (`from gnuradio import iio, qtgui, ...`, `import iio`, `from PyQt5 import ...`, `import pyqtgraph`), ob alles sauber importierbar ist, und legt drei Kommandozeilen-Starter unter `~/.local/bin/` an: `pluto-tx` (startet die GUI, `--gui` ist schon eingebaut), `pluto-rx` und `pluto-advanced-rx`. Ist `~/.local/bin` noch nicht im `PATH`, sagt das Skript das am Ende explizit dazu.

Das Skript ist beliebig oft wiederholbar (`apt-get install` auf bereits installierte Pakete ist ein No-Op) — z.B. auch einfach erneut ausführen, um nur die Starter-Skripte neu anzulegen oder den Python-Import-Test erneut laufen zu lassen.

Nicht Teil des Skripts (bewusst manuell, da hardware-/setup-abhängig):
- Der Pluto selbst muss bereits per Netzwerk (Ethernet/`ip:...`) oder USB (`usb:...`) erreichbar sein — siehe Geräte-Scan/-Auswahl unten.
- Bei einer USB-Verbindung ggf. nötige udev-Regeln für Nicht-root-Zugriff auf das USB-Gerät (in diesem Projekt bisher nicht gebraucht/getestet, siehe ToDo unten).

## Struktur

```
install.sh                  # siehe Installation oben
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
pluto_advanced_rx/
├── config.py      # eigenständige Kopie der RX-Konstanten (siehe unten) + Wasserfall-Konstanten
├── fft_probe.py    # FftProbe(gr.sync_block): numpy-FFT-Zeilen für das interaktive Wasserfall-Widget
├── flowgraph.py     # AdvancedRxFlowgraph(gr.top_block): 1:1-Kopie der pluto_rx-Demod-Kette + FftProbe-Tap
├── waterfall_widget.py  # AdvancedWaterfallWidget(QWidget): pyqtgraph-Wasserfall, Klick-zum-Tunen, Marker, Bandbreiten-Anzeige
├── gui.py             # PyQt5 GUI: wie pluto_rx, plus interaktives Wasserfall-Widget
└── app.py              # CLI-Einstieg
pluto_tx_carrier.py         # einfaches iio_attr/iio_writedev Carrier-Test-Skript (Fallback/Referenz)
```

`pluto_rx` und `pluto_advanced_rx` sind bewusst unabhängig von `pluto_tx`: der RX-Zweig kann nicht senden, braucht also keine der TX-Safety-Mechanismen (`safety.py`) und ist nur lose über die gemeinsamen Konstanten (Bandplan, Default-URI) gekoppelt. `pluto_advanced_rx` ist außerdem bewusst eine **eigenständige Kopie** von `pluto_rx` (nicht Ersatz, nicht Import) — die Demod-Kette und die RX-Konstanten sind dupliziert, damit `pluto_advanced_rx` frei weiterentwickelt werden kann, ohne die stabile `pluto_rx`-App zu berühren.

### `pluto_advanced_rx`: interaktiver Wasserfall

`pluto_rx`'s Wasserfall ist GNU Radios eigenes `qtgui.waterfall_sink_c`-Widget — ein reines C++/Qt-Objekt ohne jede Marker-/Klick-/Overlay-API. `pluto_advanced_rx` ersetzt das durch ein selbstgebautes, interaktives [pyqtgraph](https://www.pyqtgraph.org/)-Widget (`waterfall_widget.py`), im Stil von SDR++:

- **Live-Spektrumlinie oberhalb des Wasserfalls**, X-Achse (Frequenz, mit MHz-Beschriftung statt Rohwerten) mit der Wasserfall-Ansicht gekoppelt (`setXLink`) — das ist die "live Frequenzdarstellung".
- **Klick-zum-Tunen**: Klick auf Spektrum oder Wasserfall (per `ViewBox.mapSceneToView`, demselben Muster, das GNU Radios eigene gr-filter-GUI intern für Klick-Handling nutzt) retunt direkt.
- **Tuning-Marker** (senkrechte Linie) und **Demod-Bandbreiten-Anzeige** (schattierte Region um die getunte Frequenz) — beide zeigen die tatsächlich eingestellte Demodulator-Breite (siehe unten), keine Schätzung.
- **Zoom/Pan per Standard-Maus-Interaktion** (Ziehen zum Verschieben, Scrollen zum Zoomen), auf die Frequenzachse beschränkt.
- **Floor/Ceiling-Slider** (vertikal, rechts neben Spektrum+Wasserfall, übereinander): verschieben live die dB-Grenzen von Spektrum-Y-Achse und Wasserfall-Farbskala — der Rauschgrund ist je nach Antenne/Standort/Gain sehr unterschiedlich, deshalb einstellbar statt fest.

**Demodulator-Breite** (`Width (Hz)`-Regler neben der Modus-Auswahl) ist ein echter, zur Laufzeit änderbarer Filter, keine reine Anzeige: bei FM ein reelles Tiefpassfilter (`firdes.low_pass`, symmetrisch um 0 Hz auf das komplexe IF-Signal angewandt — Standardtechnik zur Bandbegrenzung eines komplexen Signals) VOR `quadrature_demod_cf`, Standard 12,5 kHz (übliche NBFM-Kanalbreite); bei SSB die Breite des bestehenden `complex_band_pass`-Demodulationsfilters selbst (untere Kante bleibt bei 300 Hz fest, die Breite verschiebt die obere Kante), Standard 3 kHz. Beide über `set_taps()` zur Laufzeit änderbar, kein Flowgraph-Rebuild nötig (dieselbe Technik, die `pluto_tx.set_mode()` schon für seinen NF-Filter nutzt).

Datenfluss: Da GNU Radios `qtgui`-Blöcke keinen Daten-Ausgang haben, berechnet ein eigener, reiner Python-`gr.sync_block` (`fft_probe.py`) die FFT direkt (numpy, gedrosselt auf `config.FFT_COMPUTE_RATE_HZ`, unabhängig von Samplerate/FFT-Größe) und hält die letzte Zeile hinter einem Lock für einen `QTimer`-Poll (`config.WATERFALL_POLL_INTERVAL_MS`) bereit.

Architektonischer Vorteil gegenüber `pluto_rx`: Da das Wasserfall-Widget ein reines Python/PyQt-Objekt ist (kein `sip.wrapinstance()`-gewrapptes C++-Objekt einer gr-qtgui-Sink), entfällt die fragile "nie `deleteLater()`, nur `setParent(None)`"-Regel aus `pluto_rx/gui.py` (dort Ursache eines echten SIGSEGV) komplett — das Widget wird einmalig gebaut und bleibt über Geräte-Reconnects UND RX-Bandbreiten-Rebuilds hinweg einfach bestehen, kein Swap-Out nötig.

RX-Bandbreiten-Presets reichen bis 10 MHz (`1/2,5/5/8/10 MHz`) — mehr als bei `pluto_rx`, aber weniger als ursprünglich angefragt (bis 20 MHz): 5/8/10 MHz laufen, zeigen aber echte Buffer-Overruns (bekannte ~4,7-4,9 MSa/s-Durchsatzgrenze der aktuellen `ip:plutoplus.local`-Verbindung, siehe unten). 15/20 MHz wurden getestet und bewusst NICHT aufgenommen: bei diesem Dezimationsverhältnis (400:1 auf `DEMOD_IF_RATE`) wird der automatisch entworfene IF-Filter >13.000 Taps lang, was den GNU-Radio-Scheduler-Puffer sprengt — kein Overrun, sondern ein Scheduler-Fehler und komplett keine Daten. Größere Puffer alleine beheben das nachweislich nicht; siehe ToDo unten.

Gilt identisch wie bei `pluto_rx`: der geteilte AD9361-RX/TX-Takt (siehe "Bekannte Einschränkungen" oben) betrifft auch `pluto_advanced_rx`, da RX-Bandbreiten-Presets ebenfalls die RX-Samplerate ändern.

## Start

Nach `./install.sh` (siehe Installation oben), mit den dabei angelegten Kurzbefehlen:

```
pluto-tx
pluto-rx
pluto-advanced-rx
```

Oder direkt, ohne die Starter-Skripte, aus dem Repo-Verzeichnis:

```
python3 -m pluto_tx.app --freq 432150000 --gui
python3 -m pluto_rx.app --freq 432150000
python3 -m pluto_advanced_rx.app --freq 432150000
```

TX ohne `--gui`: headless CLI-Test (fester Carrier für `--duration` Sekunden, oder `--interactive` für Enter-zum-Keyen).

## Sicherheitsdesign

Kernproblem, das diesen Eigenbau motiviert hat: der AD9361-Treiber (libiio) trennt Buffer-Streaming komplett von Dämpfung (`hardwaregain`) und LO-Zustand (`powerdown`) — keine SDR-Software, die wir getestet haben (SDRangel eingeschlossen), setzt diese beim Stoppen automatisch zurück. Deshalb: `PlutoSafety` (in `safety.py`) verwaltet TX-Dämpfung und LO-Powerdown komplett unabhängig von GNU Radio über rohes `python3-libiio`. `force_safe_state()` (Dämpfung auf Minimum + LO aus) ist die einzige Funktion, auf die jeder Shutdown-Pfad läuft: normales Programmende, Fenster schließen, SIGINT/SIGTERM, unbehandelte Exceptions. Die GUI zeigt zusätzlich alle 500ms den tatsächlichen Hardware-Zustand an (nicht nur den vermuteten App-Zustand) und hat einen NOTAUS-Button.

PTT schaltet nur die Dämpfung (schnell, kein LO-Relock), nicht den LO-Powerdown — der ist für App-Start/-Ende reserviert.

## Bekannte Einschränkungen

- Datei-Wechsel zur Laufzeit ("Choose File") funktioniert, ist aber kein Live-Swap: `blocks.wavfile_source` hat in dieser GNU-Radio-Version (3.10.12) keine Laufzeit-Datei-Wechsel-API, deshalb baut `MainWindow._rebuild()` bei einer neuen Dateiauswahl den kompletten Flowgraph neu auf (derselbe Mechanismus wie beim Geräte-Reconnect) — kurze Unterbrechung, aber sicher (schaltet vorher automatisch ab).
- Die Audiodatei loopt im Hintergrund unabhängig von PTT (nur per Mute gesteuert) — die Wiedergabeposition wird beim PTT-Druck nicht auf Anfang zurückgesetzt.
- **`plutoplus.local` löst kurz nach einem Kabel-/Verbindungswechsel (z.B. Ethernet → USB) manchmal noch auf die ALTE IP auf.** Der Pluto meldet sich per mDNS auf jedem aktiven Interface mit dessen eigener IP (z.B. `192.168.178.x` über Ethernet, `192.168.2.1` über den USB-Gadget-Adapter) — nach einem Wechsel dauert es ein paar Sekunden, bis `avahi-daemon` das neue Interface bemerkt und die frische mDNS-Ankündigung empfängt; bis dahin kann der Hostname noch auf die nicht mehr erreichbare alte IP zeigen ("Connect" schlägt dann fehl, sieht wie ein Verbindungsfehler aus). Selbst behoben, sobald `avahi-daemon` nachzieht (typischerweise wenige Sekunden) — zur Kontrolle: `iio_info -u ip:plutoplus.local` oder in der GUI der Scan-Button. Bis dahin notfalls die IP direkt eintragen (siehe Geräte-Scan/-Auswahl unten).
- **Der AD9361 teilt sich einen Takt zwischen RX und TX.** Läuft `pluto_rx` gleichzeitig mit `pluto_tx` (das übliche Testsetup) und ändert `pluto_rx` seine RX-Bandbreite, verstellt das nachweislich den tatsächlich von `pluto_tx` gesendeten Takt (nicht den RX-Empfang!) — gemessen als Tonhöhenverschiebung um exakt den Bandbreiten-Faktor. `pluto_rx` allein (ohne gleichzeitig laufendes `pluto_tx`) ist davon nicht betroffen — verifiziert mit einem echten, externen UKW-Sender als Referenz. Noch ungelöst, siehe ToDo unten.

`pluto_rx`'s SSB-Demodulator ist ein komplexes Bandpassfilter (`firdes.complex_band_pass`, 300-2700 Hz oberhalb der abgestimmten Frequenz) direkt auf dem RX-IQ-Signal plus `complex_to_real` — das genaue Spiegelbild von `pluto_tx`'s Hilbert-basiertem USB-Modulator. Die RX-Bandbreite ("Zoom"-Stufen, aktuell 1/2,5 MHz) bleibt hinter den Kulissen immer auf eine feste Zwischenfrequenz-Rate (`DEMOD_IF_RATE = 50 kHz`) heruntergefiltert, damit der Demod-/Resampler-Zweig unabhängig von der gewählten Zoomstufe gleich bleibt — ein Bandbreitenwechsel baut daher den kompletten Flowgraph neu auf (GNU-Radio-Filter können ihr Dezimationsverhältnis nicht zur Laufzeit ändern). 5/10 MHz sind bewusst nicht als Presets verfügbar: über die aktuelle `ip:plutoplus.local`-Verbindung (IIOD-Netzwerkprotokoll über das USB-Gadget-Interface, nicht der native USB-Backend) kommt der Durchsatz messbar nur auf ~4,7-4,9 MSa/s mit Buffer-Overruns, was sich als abgehacktes/tonhöhen-verzerrtes Audio bemerkbar macht — siehe den Netzwerk/USB-ToDo-Punkt unten.

## Gerätewahl (Netzwerk/USB)

Beide Apps haben in der GUI ein editierbares Dropdown ("Device (hostname or IP)") plus Scan- und Connect/Disconnect-Buttons. Standardmäßig wird beim Start automatisch mit `config.DEFAULT_URI` (`ip:plutoplus.local`) verbunden. Für einen zweiten Pluto im selben LAN, oder um explizit auf USB umzuschalten:

- **Scan** ruft `iio.scan_contexts()` auf (mDNS + USB + lokale IIO-Geräte, ~1s) und füllt das Dropdown mit allen gefundenen Contexts (der lokale `local:`-Eintrag, nie ein Pluto, wird herausgefiltert). Danach einfach den gewünschten Eintrag auswählen.
- Alternativ manuell eintippen: bloßer Hostname oder IP (z.B. `192.168.1.50`) — wird automatisch zu `ip:192.168.1.50` — oder eine volle libiio-URI (`usb:1.5.5`, `ip:anderer-pluto.local`, ...), die unverändert übernommen wird.

"Disconnect" fährt den Flowgraph sauber herunter (bei `pluto_tx` inklusive `force_safe_state()`, wie bei jedem anderen Shutdown-Pfad); "Connect" baut ihn mit der neuen Adresse neu auf und übernimmt dabei alle aktuellen GUI-Einstellungen (Frequenz, Modus, Gain, ...). Schlägt der Verbindungsaufbau fehl (falsche Adresse, Gerät nicht erreichbar), bleibt die App im getrennten Zustand mit einer Fehlermeldung im Statusfeld, statt abzustürzen.

Der eigentliche Verbindungsaufbau (`iio.Context`) hat kein eingebautes Timeout und kann bei manchen falschen Adressen (erreichbar auf IP-Ebene, aber ohne antwortenden IIOD) mehrere Sekunden bis potenziell sehr lange blockieren — das fühlte sich wie ein Absturz an (Fenster reagiert nicht mehr). `pluto_tx/netutil.py`'s `probe_uri_with_timeout()` prüft die Erreichbarkeit deshalb zuerst in einem Hintergrund-Thread mit 5-Sekunden-Timeout, bevor der eigentliche (Qt-Widget-erzeugende) Flowgraph überhaupt aufgebaut wird — der muss synchron im GUI-Thread bleiben, sonst entstehen Qt-Thread-Verletzungen bei den Wasserfall-Widgets (ausprobiert und wieder verworfen, siehe Git-History).

## ToDo für nächstes Mal

- **Geteilter AD9361-Takt zwischen `pluto_rx` und `pluto_tx` beheben.** Ein erster Versuch (`pluto_tx.key_ptt()` ruft vor jedem Senden `pluto_sink.set_samplerate()` erneut auf, um den Takt "zurückzuholen") hat das Problem in der Praxis NICHT gelöst — vermutlich weil das Zurückholen des Takts durch `pluto_tx` genau umgekehrt die aktuell in `pluto_rx` eingestellte Bandbreite wieder verstellt, sobald als nächstes `pluto_rx` etwas mit dem Takt macht (z.B. beim nächsten `set_frequency`/Retune, oder generell weil beide Apps denselben Takt für sich beanspruchen). Der Fix wurde deshalb wieder entfernt (siehe Git-History). Nötig ist vermutlich eine echte Recherche zu unabhängigem RX/TX-Takt auf dem AD9361/Pluto+ (Tezuka-Firmware) — z.B. ob/wie sich RX- und TX-Sample-Clock über libiio wirklich unabhängig konfigurieren lassen (eigene BBPLL-Teiler pro Richtung), oder ob das auf dieser Firmware/diesem Board grundsätzlich nicht getrennt werden kann. Bis dahin: `pluto_rx`-Bandbreite nicht ändern, während `pluto_tx` aktiv sendet.
- **Nativer USB-Backend nicht getestet** — der Geräte-Scan (siehe oben) findet auch USB-Contexts, aber ob eine echte USB-Verbindung (`usb:...`) durchgängig funktioniert, ist ungetestet — inklusive ob dafür auf einem frischen Rechner erst noch udev-Regeln für Nicht-root-Zugriff auf das USB-Gerät nötig sind (`install.sh` richtet das bewusst nicht ein). Der native USB-Backend könnte außerdem den RX-Durchsatz über die aktuell gemessenen ~4,7-4,9 MSa/s (IIOD-Netzwerkprotokoll) hinaus verbessern und 5/10-MHz-RX-Bandbreiten wieder nutzbar machen.
- **GUI besser/cooler aussehen lassen** — aktuell rein funktional (Standard-Qt-Widgets). Eventuell Inspiration von anderer SDR-Software (SDR++, SDRangel) oder modernen Ham-Radio-Interfaces holen.
- **`pluto_advanced_rx`: mehrstufige IF-Dezimation für 15/20-MHz-Presets.** Aktuell dezimiert `flowgraph.py`'s IF-Stufe in einem einzigen `rational_resampler_ccf`-Schritt von der RX-Bandbreite direkt auf `DEMOD_IF_RATE` (50 kHz). Das funktioniert bis 10 MHz (Dezimation 200:1), aber bei 15/20 MHz (400:1) wird der automatisch entworfene Filter >13.000 Taps lang und der GNU-Radio-Scheduler kann den Block nicht mehr beliefern (`ninput_items_required` > `max_possible_items_available`) — Ergebnis: keine Daten, nicht nur Overruns. Größere Puffer (`set_max_output_buffer`) alleine beheben das nachweislich nicht (getestet). Lösung wäre eine kaskadierte Dezimation (z.B. mehrere `rational_resampler_ccf`-Stufen mit je kleinerem Verhältnis statt einer einzigen mit riesigem Filter) — Standardtechnik bei großen Dezimationsverhältnissen, aber echter Umbau, kein Config-Change. Selbst mit funktionierendem Filter bliebe bei 15/20 MHz vermutlich trotzdem die Netzwerk-Durchsatzgrenze (~4,7-4,9 MSa/s) ein Problem, siehe den USB-Backend-ToDo-Punkt oben.
