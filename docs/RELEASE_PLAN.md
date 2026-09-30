# Release-Plan — pluto-tx als fertiges Paket

## Context

Heute laufen auf einem neuen Rechner bis zu **sieben Installer
nacheinander**:
- `install.sh` (apt),
- `install-m17.sh`, `install-lora.sh`, `install-rade.sh`, `install-ft8.sh`,
  `install-js8.sh` (je ein Git-Klon und ein Source-Build),
- `install-web-trx.sh` (venv, Node, Frontend-Build).

Das dauert, braucht Compiler, Node.js und Internetzugriff auf sechs
Upstream-Quellen und scheitert an Kleinigkeiten, die schon vorgekommen
sind: fehlendes g++, ein hängender Model-Download bei RADE, falsches HOME,
Starter, die sich gegenseitig die `LD_LIBRARY_PATH` überschreiben.

**Ziel:** Ein Release, das **vorgebaut** kommt, für:
- Ubuntu 26.04 LTS und Debian 13 („trixie“),
- jeweils amd64 und arm64. arm64 deckt den **Raspberry Pi** mit dem
  64-Bit-Raspberry-Pi-OS ab, das auf Debian 13 basiert.

Enthalten sind alle vier Programme (`pluto-tx`, `pluto-advanced-rx`,
`pluto-cli`, `web-trx`) und alle optionalen Komponenten (M17, RADE,
LoRa/Meshtastic/MeshCore, FT8, JS8).

**Installation so einfach wie möglich:**
- **Ubuntu Desktop:** `.deb` herunterladen und doppelklicken. Das
  App Center installiert samt Abhängigkeiten, **echtes One-Click**.
- **Überall, auch Raspberry Pi und headless:** ein einziger Befehl,
  `curl -fsSL https://jochenhammes.github.io/pluto-tx/get.sh | bash`.
  Er richtet eine signierte APT-Quelle ein und installiert das Paket;
  Updates kommen danach über `apt upgrade`.
- **Was trotzdem manuell bleibt, und warum:**
  - Ein bereits gestecktes RTL-SDR muss einmal ab- und angesteckt werden,
    weil der Kernel-DVB-Treiber es sonst belegt.
  - Die Pluto-Netzwerkadresse trägt man einmal ein.
  - Der Web-TRX-Autostart als Dienst ist ein bewusster Schritt (Senden
    ohne Anmeldung am Rechner).

„Debian/Ubuntu 26“ verstehe ich als **Ubuntu 26.04 LTS** und das aktuelle
**Debian 13**, auf dem auch Raspberry Pi OS aufsetzt. Eine
„Debian 26“-Version gibt es nicht. R0 prüft die tatsächlichen Paketstände.

Ausgeführt wird der Plan wie die bisherigen von einer Claude-Code-Session.
Die Build- und Testarbeit läuft **in der CI** (GitHub Actions, Container
der Zieldistributionen); die Abnahme läuft auf dem Radio-Server und auf
einem Raspberry Pi.

---

## 0. Regeln

- **Git-Installationen bleiben voll funktionsfähig.** `install*.sh`, die
  Entwicklung aus dem Checkout und alle bisherigen Pfade funktionieren
  weiter. Das Paket ist ein **zusätzlicher** Weg. Jede Code-Änderung dafür
  ist additiv, mit Fallback auf das heutige Verhalten.
- **Eine Quelle der Wahrheit für die Versionen:** Die gepinnten Commits
  der Fremdkomponenten stehen heute verteilt in den `install-*.sh`.
  R2 zieht sie in `packaging/components.lock`; die Installer und der
  Paketbau lesen beide daraus.
- **Senden bei Hardwaretests:** wie in `docs/JS8_PLAN.md` Abschnitt 0,
  also selbstständig, nur in Amateurfunkbändern, kleine Leistung, jede
  Aussendung protokolliert.
- **Vor jedem Commit laufen die Tests grün** wie gehabt (pluto-tx
  `unittest`, web-trx `pytest`/`ruff`, `npm run check`). Die neue
  Paket-CI kommt dazu.
- **HALT-Punkte:**
  - Ende R0 (Machbarkeit, Entscheidungen),
  - vor dem ersten veröffentlichten Release (R6),
  - bei jeder Lizenzfrage.

---

## 1. Bestand: was die Installer heute tun

| Installer | Holt / baut | Ergebnis heute | Laufzeit-Einbindung |
|---|---|---|---|
| `install.sh` | apt: gnuradio, python3-libiio, libiio-utils, avahi-daemon, python3-pyqtgraph, SoapySDR-Module HackRF/RTL-SDR, hackrf, rtl-sdr, python3-soapysdr, python3-pil, fonts-dejavu-mono, python3-serial | Systempakete; Gruppe `plugdev`; DVB-Blacklist `/etc/modprobe.d/blacklist-rtl-sdr.conf` | Starter `~/.local/bin/pluto-tx`, `pluto-advanced-rx`, `pluto-cli` |
| `install-m17.sh` | gr-m17 (gepinnt), cmake-Build, braucht gnuradio-dev | `~/.local` (lib + Python-Modul) | `LD_LIBRARY_PATH` im Starter |
| `install-lora.sh` | gr-lora_sdr `862746d…`, braucht gnuradio-dev, libvolk-dev, boost, libuhd-dev, pybind11-dev; pip meshtastic, cryptography | `~/.local` | `LD_LIBRARY_PATH` im Starter |
| `install-rade.sh` | freedv/rade_c `0d5c5f7…` inkl. gepatchtem Opus/FARGAN und ca. 100-MB-Modell-Download | `rade_c/build/src/librade.so`, `lpcnet_demo` im Checkout | `LD_LIBRARY_PATH` und `PATH` im Starter |
| `install-ft8.sh` | kgoba/ft8_lib `9fec6ca…` → `libft8wrap.so`; apt wsjtx (jt9) | `ft8_lib/` im Checkout | ctypes über den Pfad relativ zum Repo |
| `install-js8.sh` | JS8Call-improved v2.5.2 (`f0f0d01…`), JSC-Wortlisten, Build von `tools/js8ref` (Qt6-Core, FFTW, Boost) | `js8call/jsc.json`, `js8call/build-js8ref/js8ref` | Pfade relativ zum Repo |
| `install-web-trx.sh` | venv mit system-site-packages, `pip install -e web-trx/backend[dev]`, Node ≥ 18, `npm ci`/`build` | `web-trx/backend/.venv`, `web-trx/frontend/dist`, Starter `web-trx` | Laufzeitdaten in `web-trx/run/` im Checkout |
| `web-trx/scripts/install-service.sh` | systemd-Unit (Headless-Betrieb) | `/etc/systemd/system/…` | ALSA-Null-Gerät für GNU Radio ohne Login |

**Folgerungen für das Paket:**
1. gr-m17 und gr-lora_sdr sind **GNU-Radio-Out-of-Tree-Module**. Sie
   müssen gegen **genau die GNU-Radio-Version der Zieldistribution**
   gebaut sein, weil die ABI der C++-Module an die Version gebunden ist.
   Ein Build je Distribution und Architektur ist daher Pflicht; genau das
   liefert die Matrix.
2. rade_c, ft8_lib und js8ref sind normale C/C++-Bibliotheken bzw.
   -Programme, ebenfalls je Architektur gebaut.
3. Das Web-TRX-Frontend (`dist/`) ist architekturunabhängig. Es wird
   **einmal** in der CI gebaut; auf dem Zielrechner ist **kein Node.js
   mehr nötig**.
4. Mehrere Stellen schreiben heute **in den Checkout**, zum Beispiel
   `web-trx/run/`, `web-trx/web-trx.env`, `pluto_tx/_silence.wav` und die
   Build-Verzeichnisse. In einem Paket unter `/usr` ist das unmöglich;
   R1 verlegt das nach XDG-Verzeichnissen.

---

## 2. Zielbild

### Pakete

Empfehlung: **ein** Binärpaket `pluto-tx` je Ziel. Das ist am
einfachsten für One-Click und reicht für den Umfang.

| Ziel | Datei |
|---|---|
| Ubuntu 26.04 amd64 | `pluto-tx_<ver>~ubuntu26.04_amd64.deb` |
| Ubuntu 26.04 arm64 | `pluto-tx_<ver>~ubuntu26.04_arm64.deb` |
| Debian 13 amd64 | `pluto-tx_<ver>~deb13_amd64.deb` |
| Debian 13 arm64 (inkl. Raspberry Pi OS 64 bit) | `pluto-tx_<ver>~deb13_arm64.deb` |

32-Bit-Raspberry-Pi-OS (armhf) ist **nicht** Teil von 1.0. Es gibt
keinen nativen CI-Runner dafür, und GNU Radio auf 32 bit ist dort
schwach; es wird nach 1.0 geprüft.

**Abhängigkeiten** (in `debian/control`, apt löst sie beim Installieren
aus den Distributionsquellen auf):
- `Depends:` gnuradio, python3-pyqt5, python3-pyqtgraph, python3-numpy,
  python3-libiio, libiio-utils, avahi-daemon, soapysdr-module-hackrf,
  soapysdr-module-rtlsdr, python3-soapysdr, hackrf, rtl-sdr, python3-pil,
  fonts-dejavu-mono, python3-serial, python3-cryptography, die Laufzeit-Libs
  von volk, boost, fftw3 und Qt6-Core (automatisch per `${shlibs:Depends}`).
- `Recommends:` wsjtx (jt9, bester FT8-Decoder).
- `Suggests:` js8call.

### Dateilayout im Paket

```
/usr/bin/pluto-tx, pluto-advanced-rx, pluto-cli, web-trx     kleine Starter (Python-Pfade, sonst nichts)
/usr/share/pluto-tx/                  Python-Code: pluto_tx/, pluto_advanced_rx/, pluto_cli/, web-trx/backend/web_trx/
/usr/share/pluto-tx/web-trx/dist/     vorgebautes Frontend
/usr/lib/pluto-tx/                    architekturabhängig, privat (keine Konflikte mit Distributionspaketen):
    lib/        libft8wrap.so, librade.so, libgnuradio-m17.so*, libgnuradio-lora_sdr.so*
    bin/        lpcnet_demo, js8ref
    python/     gnuradio/m17, gnuradio/lora_sdr (Python-Teile der OOT-Module), meshtastic + Abhängigkeiten,
                die nicht in der Distribution sind
    web-trx/venv/   nur falls R0 zeigt, dass die Distributionspakete für FastAPI/Uvicorn zu alt sind
/usr/share/pluto-tx/js8/jsc.json      JSC-Wortlisten
/usr/share/applications/*.desktop, /usr/share/icons/hicolor/…   Menüeinträge für die drei GUI-/Web-Programme
/usr/lib/udev/rules.d/60-pluto-tx.rules   HackRF/RTL-SDR mit TAG+="uaccess" (Zugriff für den angemeldeten
                                          Benutzer ohne Gruppe und ohne Neu-Login)
/usr/lib/modprobe.d/pluto-tx-blacklist-rtl.conf   DVB-Blacklist (statt Datei in /etc schreiben)
/usr/lib/systemd/system/web-trx@.service  Headless-Dienst als Template, deaktiviert ausgeliefert
/usr/share/doc/pluto-tx/                  README, Changelog, Lizenzen aller Komponenten
```

**Laufzeitdaten pro Benutzer (XDG):**
- `~/.config/pluto-tx/`: `web-trx.env`, Einstellungen.
- `~/.local/state/web-trx/`: `run/`, Log, PID, TLS, Sessions,
  `settings.json`, TX-Log.
- `~/.local/share/web-trx/`: JS8-Inbox (schon heute dort).
- `~/.cache/pluto-tx/`: z. B. `_silence.wav`.

Die Native-Libs der OOT-Module werden mit **RPATH** auf
`/usr/lib/pluto-tx/lib` gebaut. Kein Starter muss mehr `LD_LIBRARY_PATH`
zusammenstückeln, und die Merge-Logik der heutigen Installer entfällt
für das Paket.

### Installationswege

1. **Ubuntu Desktop, One-Click:** `.deb` von der Release-Seite laden und
   doppelklicken. Das App Center zeigt das Paket und installiert es samt
   GNU Radio usw.
2. **Ein Befehl, alle Ziele:** `get.sh` (siehe R4). Es erkennt
   Distribution und Architektur, legt den Signaturschlüssel nach
   `/etc/apt/keyrings/`, die Quelle nach
   `/etc/apt/sources.list.d/pluto-tx.sources`, und führt
   `apt install pluto-tx` aus. Danach kommen Updates über
   `apt upgrade`.
3. **Offline bzw. von Hand:** `sudo apt install ./pluto-tx_…deb`.
4. **Entwicklung:** Git-Checkout und `install*.sh`, unverändert.

---

## 3. Phasen

### R0 — Bestandsaufnahme und Machbarkeit (HALT am Ende)

In Containern `ubuntu:26.04` und `debian:trixie`, je amd64 und arm64,
dazu auf einem echten Raspberry Pi mit Raspberry Pi OS (64 bit).

1. **Paketstände festhalten** in `docs/release/R0.md`:
   - gnuradio (Version, ob gnuradio-dev vorhanden),
   - python3-pyqt5, pyqtgraph, soapysdr, libiio,
   - Qt6-Core-dev, fftw, boost, volk, uhd, pybind11,
   - Python-Version,
   - python3-fastapi / -uvicorn / -websockets / -numpy, verglichen mit
     `web-trx/backend/pyproject.toml`,
   - ob es **gr-m17 oder gr-lora-sdr schon als Distributionspakete** gibt.
     Falls ja und die Version passt: davon abhängen statt selbst bauen.
   - ob wsjtx und js8call in allen vier Zielen vorhanden sind.
   - Raspberry Pi OS: bestätigen, dass dessen gnuradio-Paket dasselbe wie
     in Debian 13 arm64 ist; dann passt ein Build für beide.
2. **Probe-Builds** aller Komponenten mit den gepinnten Commits, in allen
   vier Zielen. Festhalten:
   - Dauer,
   - Größe der Ergebnisse (RADE-Modell!),
   - ob der RADE-Build Netz braucht (Opus-Fork, Modell-Download). Wenn ja:
     in der CI **einmal** herunterladen, mit Prüfsumme im Build-Cache
     halten, und im Paket nur das Ergebnis ausliefern.
3. **CI-Runner:**
   - Ist das Repo öffentlich? Dann gibt es kostenlose native
     arm64-Runner (`ubuntu-24.04-arm`) und Docker für die
     Zielcontainer.
   - Sonst: arm64 per QEMU (Dauer messen) oder ein selbst gehosteter
     Runner auf einem Raspberry Pi 5.
4. **Lizenz-Audit.** Welche Lizenz hat jede gebündelte Komponente, und
   ist die Kombination erlaubt? Zu prüfen:
   - pluto-tx: GPLv3,
   - gr-lora_sdr: GPLv3,
   - JS8Call-Code und Wortlisten: GPLv3,
   - rade_c: BSD-2,
   - ft8_lib: MIT,
   - KissFFT: BSD,
   - Opus/FARGAN-Modell: Lizenz der Gewichte prüfen,
   - Python-Pakete aus PyPI,
   - **gr-m17: Die README spricht von GPLv2. Genau prüfen, ob „v2 or
     later“.** Bei „v2 only“ ist das Mitbündeln mit GPLv3-Code
     problematisch; dann gr-m17 als eigenes Paket mit eigener Lizenz
     ausliefern und als separates Programmteil laden, oder weglassen und
     den Installer behalten → **HALT**, Betreiber entscheidet.

   Für die GPL-Komponenten gehören die **passenden Quelltexte** als
   Release-Assets dazu (`sources-<ver>.tar.xz`).
5. **Audit „schreibt in das Programmverzeichnis“:** alle Stellen, die
   relativ zu `__file__` oder zum Repo schreiben oder Pfade erwarten.
   Grep auf `__file__`, `_REPO_LIB`, `web-trx/run`, `rade_c/build`,
   `ft8_lib/`, `js8call/`, `_silence.wav`, `.venv`. Die Liste geht in
   `R0.md`.
6. **Raspberry Pi: Leistung messen** (Pi 4 und Pi 5, falls vorhanden)
   mit den Git-Installern. Gemessen werden Wasserfall-Zeilenrate, die
   CPU-Last im RX mit FM, SSB, FT8-Decode, JS8-Decode, RADE (Echtzeit?)
   und LoRa, sowie das Web-TRX-Streaming. Das Ergebnis bestimmt die
   Hinweise in der Doku, etwa „RADE auf dem Pi 4 nicht in Echtzeit“.

**HALT:**
- Zielmatrix,
- Build-Weg je Komponente (selbst bauen oder Distributionspaket),
- Web-TRX aus Distributionspaketen oder mit eigener venv,
- Lizenzbefund, besonders gr-m17,
- Runner-Weg,
- Pi-Messwerte.

### R1 — Code für ein installiertes Layout vorbereiten (additiv)

1. Ein neues Modul `pluto_tx/paths.py` wird die **einzige Quelle für
   Ressourcenpfade**. Reihenfolge:
   1. Umgebungsvariable (z. B. `PLUTO_TX_LIBDIR`),
   2. Checkout-Pfade wie heute (`ft8_lib/`, `rade_c/build/src`,
      `js8call/…`),
   3. Paketpfade (`/usr/lib/pluto-tx/…`, `/usr/share/pluto-tx/…`).

   Umstellen darauf: `ft8_ctypes` (TX und RX), `rade_ctypes`,
   `lpcnet_subprocess`, `js8_decoder` (js8ref), `js8_message` (jsc.json),
   die Erkennung der OOT-Module und `features` in Web-TRX.
2. **Schreibbare Orte** kommen über `paths.user_dir(kind)` nach XDG:
   config, state, cache, data.
   - `_silence.wav` landet im Cache.
   - Web-TRX: `WEB_TRX_RUN_DIR` (Standard: im Checkout wie heute, im Paket
     `~/.local/state/web-trx`) und `WEB_TRX_ENV_FILE` (im Paket
     `~/.config/pluto-tx/web-trx.env`).
   - `start.sh`, `stop.sh`, `status.sh` und `pluto_tx/webtrx_control.py`
     lesen diese Orte statt fester Pfade relativ zu `web-trx/`.
3. **Versionsnummer:**
   - `pluto_tx/__init__.py`: `__version__` (aus Git-Tag bzw. Paket),
   - `--version` in allen vier Programmen,
   - „Über“-Dialog bzw. Anzeige in den Apps,
   - `version` in `/health`.
4. **Erststart-Hinweis in den Apps** (nicht blockierend): Welche
   optionalen Komponenten sind da (M17, RADE, LoRa, FT8, JS8)? Im Paket
   sind alle „vorhanden“. Im Checkout zeigt der Hinweis, welches
   `install-*.sh` fehlt.
5. **Web-TRX-Dienst** (`web-trx@.service`): Die bestehende
   `install-service.sh`-Logik wird als Template-Unit für einen Benutzer
   übernommen, inkl. ALSA-Null-Gerät. Zum Einschalten gibt es
   `web-trx service enable|disable`, weiterhin bewusst manuell.

**Tests R1:**
- Neue `tests/test_paths.py`:
  - Die Reihenfolge Umgebungsvariable → Checkout → Paket stimmt, mit
    Temp-Verzeichnissen.
  - Ohne Paketpfade verhält sich alles wie heute.
  - Die XDG-Pfade folgen den XDG-Variablen.
- `test_webtrx_control` in einer Variante mit Paket-Layout:
  run- und env-Datei außerhalb von `web-trx/`, der ganze
  Start/Stop/Status-Zyklus.
- **Alle bestehenden Tests unverändert grün**, der FT8-Prüfsummentest
  eingeschlossen.
- `--version` ist in allen vier Programmen vorhanden.

### R2 — Paketbau

1. `packaging/components.lock` mit URL, Commit und Prüfsumme für
   gr-m17, gr-lora_sdr, rade_c (inkl. Opus-Fork und Modell), ft8_lib,
   JS8Call-improved und den PyPI-Paketen (meshtastic mit festen
   Versionen). Die `install-*.sh` lesen ihre Pins **ab jetzt auch
   daraus**, mit identischen Werten; ein Test vergleicht beide.
2. `packaging/build-components.sh <prefix>` baut alle
   Native-Komponenten in ein Staging-Verzeichnis. Es wiederverwendet die
   bewährten Build-Schritte der Installer: den wget-Timeout-Wrapper von
   RADE, die Ergänzung der KissFFT-Objekte bei ft8_lib und
   `tools/js8ref/build.sh`. Neu sind nur Ziel-Prefix und RPATH. Es
   installiert nichts ins System und läuft im Container.
3. `packaging/debian/`: `control`, `rules` (debhelper), `changelog`,
   `copyright` (maschinenlesbar, alle Komponenten), `postinst`
   (`udevadm trigger`, einen Hinweis zum Ab- und Anstecken des RTL-SDR,
   **kein** Benutzereingriff), `postrm`, `pluto-tx.lintian-overrides`
   (private Libs in `/usr/lib/pluto-tx`).
4. Die Starter in `/usr/bin` setzen nur `PYTHONPATH`
   (`/usr/share/pluto-tx:/usr/lib/pluto-tx/python`) und starten
   `python3 -m …`.
5. **Web-TRX:** `dist/` aus dem Frontend-Build-Schritt. Das Backend nutzt
   entweder die System-Python-Pakete oder, je nach R0-Entscheidung, eine
   venv mit festen Wheels, im Build erstellt mit
   `--system-site-packages`, damit GNU Radio sichtbar ist.
6. **Kollisionsschutz mit Git-Installationen:** Liegen alte Starter in
   `~/.local/bin`, verdecken sie `/usr/bin`, weil `~/.local/bin` im PATH
   zuerst steht.
   - `pluto-tx --version` bzw. das Paket-`postinst` können das nicht für
     jeden Benutzer prüfen.
   - Deshalb warnt ein Erststart-Check in den Apps.
   - Und `pluto-tx-doctor` (s. u.) zeigt es an und bietet an, die alten
     Starter umzubenennen.
7. **`pluto-tx-doctor`:** ein kleines Diagnoseprogramm im Paket. Es
   meldet, wer im PATH gewinnt, welche Komponenten gefunden werden, die
   udev- und uaccess-Rechte, ob der DVB-Treiber noch geladen ist, die
   gefundenen SDRs (`SoapySDRUtil`, `iio_info -s`), NTP für FT8/JS8,
   Web-TRX-Status und Port.

**Tests R2:**
- `lintian` ohne Fehler; Warnungen dokumentiert oder begründet
  überschrieben.
- `debian/copyright` deckt jede Datei ab; eine Prüfung gegen die
  Komponentenliste ist Teil der CI.
- Reproduzierbarkeit: Zwei Builds desselben Commits ergeben bis auf
  Zeitstempel identische Inhalte (`diffoscope`, informativ).

### R3 — CI: bauen, installieren, testen, veröffentlichen

Neuer Workflow `.github/workflows/release.yml`. Der bestehende
`web-trx.yml` bleibt unverändert.

**Auslöser:**
- Bei jedem PR und Push auf `main`: **bauen und testen**, aber nicht
  veröffentlichen.
- Bei einem Tag `v*`: zusätzlich veröffentlichen.

**Jobs:**
1. `frontend`: einmal `npm ci && npm run build` ergibt das Artefakt
   `web-trx-dist`.
2. `build` (Matrix: 4 Ziele):
   - läuft im Zielcontainer (arm64 auf nativen arm64-Runnern bzw. nach
     R0-Entscheidung),
   - `build-components.sh` mit Cache je Ziel und Lock-Prüfsumme,
   - dann `dpkg-buildpackage`, `lintian`.
   - Ergebnis: `.deb` als Artefakt.
3. **`install-test`** (Matrix: 4 Ziele), **frischer** Container ohne
   Build-Werkzeuge:
   - `apt-get install ./pluto-tx_*.deb`, das muss allein aus den
     Distributionsquellen auflösen.
   - Smoke:
     - `pluto-cli --version`, `pluto-cli --help`,
     - `python3 -c "from gnuradio import m17, lora_sdr"` über den
       Starter-Pfad,
     - ctypes-Laden von `libft8wrap.so` und `librade.so`,
     - `lpcnet_demo` und `js8ref` starten,
     - JS8-Codec-Selbsttest; `js8ref` dekodiert
       `tests/data/js8/wav/cq_sm0_f0.wav`,
     - FT8 Encode → Decode mit ft8_lib,
     - RADE: Rundlauf aus kurzer WAV,
     - `pluto-tx-doctor`.
   - GUI: `QT_QPA_PLATFORM=offscreen`, beide `MainWindow("x")`
     konstruieren und die Web-TRX-Zeile prüfen.
   - Web-TRX:
     - `web-trx start` mit `WEB_TRX_BACKEND=sim`, `/health` antwortet mit
       `version`,
     - Login,
     - Playwright-Smoke (JS8-Loopback, FT8-Seite),
     - `web-trx stop`,
     - die Laufzeitdaten liegen unter `~/.local/state/web-trx`, in
       `/usr` wird nichts geschrieben: vorher/nachher
       `find /usr -newer` leer.
   - **Die volle pluto-tx-Testsuite** läuft gegen das installierte Paket
     (`python3 -m unittest discover tests` aus dem Checkout, aber mit
     `PYTHONPATH` auf das Paket). Damit läuft sie **erstmals in der CI**,
     denn im Container ist GNU Radio vorhanden.
   - `apt-get remove`, dann `purge`: Es bleiben keine Dateien in `/usr`
     übrig.
4. `upgrade-test` (ab dem zweiten Release): vorheriges Release
   installieren, auf das neue aktualisieren, Smoke erneut; Daten in
   `~/.local/state` bleiben.
5. **`publish`** (nur bei Tag, nach grünen Tests):
   - GitHub Release mit den vier `.deb`, `SHA256SUMS` (signiert),
     `sources-<ver>.tar.xz` (Quelltexte aller gebündelten Komponenten),
     Release Notes aus `debian/changelog`,
   - APT-Repository auf GitHub Pages (`reprepro` oder `aptly`, signiert
     mit einem eigenen Release-Schlüssel als Actions-Secret),
   - `get.sh`.

**Tests R3:** Der Workflow selbst ist der Test. Dazu kommt ein
Probe-Tag `v0.9.0-rc1`, der ein **Pre-Release** erzeugt (nicht
„latest“).

### R4 — One-Command-Installer und Doku

1. `packaging/get.sh`, veröffentlicht unter
   `https://jochenhammes.github.io/pluto-tx/get.sh`:
   - erkennt Distribution und Architektur (`/etc/os-release`,
     `dpkg --print-architecture`); nicht unterstützte Ziele werden klar
     abgelehnt, mit Hinweis auf den Git-Weg,
   - holt den Schlüssel und prüft den Fingerprint, der im Skript steht,
   - schreibt die deb822-Quelle,
   - `apt-get update && apt-get install -y pluto-tx`,
   - ruft danach `pluto-tx-doctor` auf,
   - ist idempotent,
   - `--uninstall` entfernt Paket, Quelle und Schlüssel.

   `curl … | bash` ist bequem, aber blind. Die Doku nennt immer auch den
   Weg „erst ansehen, dann ausführen“ und den reinen `.deb`-Weg.
2. **README neu gliedern:**
   - **Installation für Anwender** (drei Wege aus Abschnitt 2) zuerst,
   - **Installation aus dem Quelltext** (die bisherigen Installer) als
     eigener Abschnitt für Entwickler,
   - ein Abschnitt „Raspberry Pi“ mit den R0-Messwerten und Empfehlungen,
   - ein Abschnitt „Headless / Web-TRX als Dienst“.
3. `docs/release/RELEASING.md` beschreibt, wie ein Release entsteht:
   Changelog, Version, Tag, was die CI macht, wie man zurückzieht.

**Tests R4:** Frische Container und VMs für alle vier Ziele:
- `get.sh` installiert,
- ein zweiter Lauf ändert nichts,
- `--uninstall` räumt vollständig auf,
- ein manipulierter Schlüssel-Fingerprint lässt `get.sh` abbrechen.

### R5 — Abnahme auf echter Hardware

| Test | Umgebung | Inhalt | Kriterium |
|---|---|---|---|
| A1 frisch, Ubuntu Desktop | Ubuntu 26.04 amd64 (VM oder Rechner, frisch) | `.deb` per Doppelklick im App Center | Installiert ohne Terminal; alle drei Menüeinträge starten; `pluto-tx-doctor` grün |
| A2 frisch, Raspberry Pi | Raspberry Pi 4 oder 5, frisches Raspberry Pi OS 64 bit | `get.sh`; RTL-SDR RX: FM, SSB, FT8 (KW via Direct Sampling, falls Antenne), JS8; Web-TRX im Browser eines anderen Rechners | Alles startet; Leistung wie in R0 gemessen, dokumentiert |
| A3 Umstieg Radio-Server | bestehende Git-Installation | Paket installieren, `pluto-tx-doctor` zeigt die alten Starter, Umbenennen; Web-TRX-Laufzeitdaten aus `web-trx/run/` nach `~/.local/state/web-trx` übernehmen (Skript, siehe unten); Login bleibt, TX-Log und Einstellungen bleiben | Nichts geht verloren; der Git-Checkout funktioniert parallel weiter |
| A4 Senden | Radio-Server und Pi mit Pluto | FM, FT8, JS8 (2 m, Pluto ≥ 40 dB Dämpfung, wie JS8-Plan Abschnitt 0), RTL-SDR empfängt | Wie in den bisherigen Funktests; NOTAUS und Safe-State per `iio_attr` |
| A5 Headless | Pi ohne Desktop | `web-trx service enable`, Neustart, Zugriff im Browser ohne Anmeldung am Pi | Dienst läuft nach dem Boot, sendet ohne Browser nichts (Watchdogs) |
| A6 Update | alle | Release N → N+1 per `apt upgrade` | Laufzeitdaten bleiben, Dienst läuft danach wieder |

Für A3 kommt das Umzugsskript `packaging/migrate-from-git.sh` dazu, als
schlanke Version der bewährten `50_migrate_runtime.sh`: kopieren statt
verschieben, Prüfsummen, Trockenlauf als Standard.

### R6 — Release 1.0.0 (HALT davor)

- Changelog und Release Notes: Was ist drin, bekannte Grenzen
  (Pi-Leistung, armhf fehlt, KW-Senden nur HackRF, …).
- Tag `v1.0.0`; die CI veröffentlicht. Danach eine Prüfung aus Sicht
  eines Außenstehenden: Release-Seite, `get.sh` auf frischem Ubuntu und
  frischem Pi.
- Das GitHub-Repo bekommt einen „Releases“-Hinweis in der README-Kopfzeile.

---

## 4. Warum nicht AppImage, Flatpak, Snap oder Docker

| Weg | Problem hier |
|---|---|
| AppImage | GNU Radio mit Python, Qt und SoapySDR-Treibern in ein Image zu packen, ist groß und fragil. udev-Regeln und die DVB-Blacklist brauchen trotzdem Root. |
| Flatpak / Snap | USB- und Netzwerkzugriff auf SDRs, udev, Kernelmodul-Blacklist und der systemd-Dienst widersprechen der Sandbox. GNU Radio gibt es dort nicht als gepflegte Laufzeit. |
| Docker | Keine sinnvolle Desktop-GUI; USB-Weitergabe umständlich. Allenfalls für den Web-TRX-Headless-Betrieb interessant, später prüfbar. |
| **.deb + APT-Repo** | Nutzt die GNU-Radio-Pakete der Distribution, udev/modprobe/systemd sind vorgesehen, Updates laufen über `apt`, und Ubuntu installiert `.deb` per Doppelklick. |

---

## 5. Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| GNU-Radio-Version der Distribution ändert sich in einem Punkt-Update, die OOT-Module brechen | `Depends: gnuradio (>= X.Y.Z), gnuradio (<< X.Y+1)` bzw. exakt auf die ABI der Build-Version; bei einem Update neu bauen (CI-Matrix); `pluto-tx-doctor` erkennt einen Import-Fehler |
| RADE-Build braucht Netz bzw. das Modell ist groß | Einmal in der CI mit Prüfsumme holen und cachen; das Paket enthält nur Ergebnisse; Größe in R0 festhalten |
| gr-m17 „GPLv2 only“ | R0-Lizenz-Audit, HALT; notfalls als eigenes Paket oder weiter über den Installer |
| Ubuntu 26.04 und Debian 13 haben unterschiedliche Paketnamen (z. B. t64-Umbenennungen) | Eigene `control`-Varianten je Ziel, generiert aus einer Vorlage; der Install-Test in frischen Containern deckt Namensfehler auf |
| arm64-Builds in der CI teuer oder langsam | R0 misst; notfalls ein selbst gehosteter Runner auf einem Pi 5 |
| Alte Git-Starter in `~/.local/bin` verdecken das Paket | `pluto-tx-doctor` und der Erststart-Check warnen und benennen auf Wunsch um |
| Schreibzugriffe ins Programmverzeichnis übersehen | R0-Audit plus Install-Test (`find /usr -newer` leer) |
| Raspberry Pi zu langsam für einzelne Modi | R0-Messung, klare Doku, ggf. Modus-Hinweise in den Apps |
| `curl … \| bash` wird als unsicher empfunden | Signiertes Repo, Fingerprint im Skript, alternativ reiner `.deb`-Weg und „erst lesen“-Anleitung |
