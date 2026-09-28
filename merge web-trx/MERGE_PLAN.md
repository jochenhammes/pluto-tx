# Web-TRX in pluto-tx zusammenführen — ausführbarer Plan

Dieser Plan wird **lokal auf dem Radio-Server** von einer Claude-Code-Session
ausgeführt (dort, wo `~/Dokumente/plutosdr` und `~/Dokumente/Web-TRX`
liegen und die SDRs hängen). Er ist so geschrieben, dass jede Phase aus
fertigen Skripten besteht; die Session ruft sie auf, liest die Ausgabe,
hält an den **HALT**-Stellen an und berichtet.

Ziel: Web-TRX wird als Ordner `web-trx/` Teil von pluto-tx, gleichwertig
neben `pluto-tx`, `pluto-advanced-rx` und `pluto-cli`. Das Submodule
entfällt, TX- und RX-App bekommen eine Zeile „Web-TRX“ mit
Start/Stop/Open. **Nichts darf kaputtgehen**: Apps, Installer, Starter,
Tests, Produktiv-Checkout, Laufzeitdaten (Passwort, Anmeldungen, TX-Log,
Einstellungen, Zertifikat) und die Sicherheit der Sender.

Entscheidungen des Betreibers (fest):
- Ein einziger Import-Commit; die Einzelhistorie bleibt im archivierten
  Web-TRX-Repo.
- Nach dem Merge ist das ganze Repo frei bearbeitbar; „pluto-tx read-only“
  gilt nicht mehr.
- Der Server läuft weiter, wenn eine App geschlossen wird.

---

## 0. Regeln für die ausführende Session

1. **Phase für Phase.** Keine Phase überspringen, keine vorziehen. An jedem
   **HALT** anhalten, das Ergebnis kurz berichten (was lief, was
   auffällig war) und auf die Freigabe des Betreibers warten.
2. **Skripte statt Handarbeit.** Die Skripte prüfen Vorbedingungen und
   brechen lieber ab, als zu raten. Bricht eines ab: Ausgabe lesen,
   berichten, **nicht improvisieren**. Kein Skript „reparieren“, um einen
   Abbruch zu umgehen.
3. **Bestätigungen:** Manche Schritte fragen nach. Ohne Terminal bricht das
   Skript dann mit „Bestätigung nötig: …“ ab. Die Frage wörtlich an den
   Betreiber geben und erst nach seinem Ja mit `--yes` erneut aufrufen.
4. **Nie:** `git push --force`, `git reset --hard`, `rebase` auf `main`,
   `git add -A` im Produktiv-Checkout, Dateien unter `run/` löschen oder
   verschieben (nur kopieren), `~/Dokumente/Web-TRX` löschen, senden ohne
   ausdrückliche Freigabe (Frequenz, Leistung und Modus nennt der Betreiber).
5. **Produktiv-Checkouts** werden nur an diesen Stellen verändert:
   - M2: lokaler Tag `pre-web-trx-merge`,
   - M5: `git pull --ff-only`,
   - M6: Laufzeitdaten kopieren, `install-web-trx.sh`.

   Gebaut und getestet wird in Scratch-Klonen unter `~/merge-web-trx/work`.
6. **Keine WEB_TRX_\*-Variablen** in der Shell (der Preflight prüft das).
   Tests laufen mit `PLUTO_WEBTRX_CONTROL=off`, damit sie nie mit dem
   Produktiv-Server reden; die neuen Web-TRX-Tests schalten es selbst ein.
7. Logs: `~/merge-web-trx/logs/`, Testergebnisse: `~/merge-web-trx/results/`,
   Sicherungen: `~/merge-web-trx/backup/`. Alles außerhalb der Repos.

Kurzform für die Befehle unten:

```bash
P=~/Dokumente/plutosdr          # pluto-tx, Produktiv-Checkout
M="$P/merge web-trx/scripts"    # die Merge-Skripte (bis M2)
T=~/merge-web-trx/tools/scripts # dieselben Skripte als Kopie außerhalb des Repos (ab M2, nach dem Merge die einzige)
```

---

## 1. Was sich ändert — und was nicht

| Bereich | Vorher | Nachher |
|---|---|---|
| Web-TRX-Code | eigenes Repo `~/Dokumente/Web-TRX`, pluto-tx als Submodule `vendor/pluto-tx` | `~/Dokumente/plutosdr/web-trx/`, importiert pluto-tx aus demselben Checkout |
| Start/Stopp | `~/Dokumente/Web-TRX/scripts/start.sh` / `stop.sh` | `web-trx start/stop/restart/status/open/log` bzw. `web-trx/scripts/*.sh`, **zusätzlich** Zeile „Web-TRX“ in beiden Apps |
| Laufzeitdaten | `~/Dokumente/Web-TRX/run/`, `web-trx.env` | `~/Dokumente/plutosdr/web-trx/run/`, `web-trx/web-trx.env` (kopiert in M6) |
| Installation | README-Schritte von Hand | `./install-web-trx.sh` (optional, wie `install-m17.sh`) |
| `install.sh`, `install-*.sh`, Starter `pluto-tx`/`pluto-advanced-rx`/`pluto-cli` | — | **unverändert** |
| TX-/RX-App | — | nur **Einfügungen** in `gui.py` (keine Zeile gelöscht); ohne installiertes Web-TRX zeigt die Zeile „not installed“ |
| `/health` | `{"status","backend"}` | für Anfragen von 127.0.0.1 zusätzlich `devices` |
| CI | Web-TRX-Repo | `.github/workflows/web-trx.yml` in pluto-tx (erste CI dort) |

Neue bzw. geänderte Dateien in pluto-tx (sonst nichts, `40_verify.sh`
erzwingt das):
- `web-trx/**`
- `.github/workflows/web-trx.yml`
- `install-web-trx.sh`
- `pluto_tx/webtrx_control.py` und `pluto_tx/webtrx_widget.py`
- die beiden `gui.py` (nur Einfügungen)
- `tests/test_webtrx_*.py`
- `README.md`

Außerdem wird `merge web-trx/**` gelöscht.

Die Gerätekonflikt-Regel (Web-TRX und eine App nie gleichzeitig auf
demselben SDR) setzen drei Stellen in den Apps um:
1. **Start** mit offenem Gerät: Die App fragt nach und bietet *Trennen und
   starten*, *Trotzdem starten (anderes Gerät)* oder *Abbrechen* an. Sie
   nennt dabei die Geräte, die Web-TRX beim Start wieder öffnet.
2. **Connect**: Die App warnt nur, wenn der zuletzt abgefragte Status sagt,
   dass Web-TRX genau diesen Gerätetyp offen hat.
3. **Automatische Verbindung beim App-Start**: Sie entfällt ohne Dialog,
   wenn Web-TRX den Gerätetyp hält; die Statuszeile sagt warum.

---

## 2. Phasen

### M0 — Einfrieren und Preflight

Vorher mit dem Betreiber klären:
- Keine offene Arbeit in beiden Repos, alles gepusht.
- FT8 F2 wird erst nach dem Merge fortgesetzt.
- Ein Zeitfenster ohne Funkbetrieb, ca. 1–2 Stunden.

```bash
mkdir -p ~/merge-web-trx
cp "$P/merge web-trx/merge.env.example" ~/merge-web-trx/merge.env   # optional, Standardwerte passen normalerweise
"$M/00_preflight.sh"
```

Erwartet: **„Alle Prüfungen bestanden.“**

Diese Warnungen sind in Ordnung:
- alter Server läuft (bis M5),
- Browser-Verbindungen (bis M5),
- `shellcheck`/`ss` fehlen (dann entfallen nur diese Prüfungen).

Gibt es ein **FAIL** → **HALT**, berichten.

Hinweis: Der Preflight führt in beiden Repos `git fetch` aus. Das ändert
keinen Branch und kein Arbeitsverzeichnis.

### M1 — Baseline

```bash
"$M/10_baseline.sh"        # ca. 5–15 Minuten
```

Legt Scratch-Klone unter `~/merge-web-trx/work/baseline` an. `rade_c/`,
`ft8_lib/` und die anderen nativen Builds werden verlinkt, nicht kopiert.
Die Tests laufen dort:
- pluto-tx mit `unittest`, Ergebnis je Test-ID,
- das alte Web-TRX-Backend mit dem alten venv,
- das Frontend.

Ergebnis in `results/baseline-summary.txt`.

**HALT:** Zusammenfassung berichten. Rote Tests sind *vorbestehend*. Sie
blockieren nicht, aber der Betreiber soll sie kennen, denn später darf
nichts **schlechter** werden.

### M2 — Sicherung

```bash
"$M/20_backup.sh"
```

Legt unter `~/merge-web-trx/backup/<Zeit>/` an:
- Git-Bundles beider Repos (geprüft),
- `runtime.tgz` mit `web-trx.env` und `run/`,
- Kopien der Starter,
- `old-venv-freeze.txt` (die Paketstände des alten venvs, für M6),
- `SHA256SUMS`.

Außerdem:
- Setzt den lokalen Tag `pre-web-trx-merge` auf pluto-tx `HEAD`.
- Kopiert diesen ganzen Ordner nach `~/merge-web-trx/tools/`. **Ab hier
  immer `$T/...` verwenden**, denn der Merge löscht `merge web-trx/` aus
  dem Checkout.

Prüfen: `cat ~/merge-web-trx/backup/LATEST` zeigt das Verzeichnis, und
`ls ~/merge-web-trx/tools/scripts` listet die Skripte.

### M3 — Merge bauen (nur im Scratch-Klon)

```bash
"$T/30_build_merge.sh"            # nach einem Abbruch: "$T/30_build_merge.sh" --fresh
```

Klont `$P` nach `~/merge-web-trx/work/pluto-tx`, Branch `merge-web-trx`,
und erzeugt acht Commits:

1. **Import:** `git archive` des Web-TRX-Commits nach `web-trx/`, ohne
   `vendor/`, `.gitmodules`, `.github/` und `LICENSE`. Die Lizenz ist
   identisch mit der von pluto-tx, das wird per `cmp` geprüft.
2. **CI-Workflow.**
3. **Backend:**
   - `pluto_path` auf die Repo-Wurzel,
   - `/health` mit `devices`,
   - `/health` nicht im Access-Log,
   - dazu Tests.
4. **Skripte:**
   - `start.sh` und `stop.sh` mit `flock`, PID-Prüfung über `/proc` und
     eigenen Exit-Codes,
   - `status.sh`,
   - `install-web-trx.sh`.
5. **`pluto_tx/webtrx_control.py`** mit Tests.
6. **`pluto_tx/webtrx_widget.py`**, der Einbau in beide `gui.py` und die
   Tests.
7. **Doku.**
8. **Kommentare**, dazu wird `merge web-trx/` gelöscht.

Nach jedem Commit läuft `40_verify.sh --quick`.

Die Patches sind gegen den Stand in `BASE` geschrieben. Haben sich die
Repos seitdem bewegt, wendet das Skript sie per 3-Wege-Merge an und warnt.
Bei **Konflikten** bricht es ab → **HALT**. Die Anleitung zum Einarbeiten
von Hand steht in `patches/README.md` (Ankertexte). Danach mit `--fresh`
neu bauen, nie im halbfertigen Klon weitermachen.

### M4 — Verifikation

```bash
"$T/40_verify.sh"
```

Ohne Hardware, im Scratch-Klon. Geprüft wird:

1. Nur Dateien aus der Liste in Abschnitt 1 wurden geändert.
2. In beiden `gui.py` gibt es **keine gelöschte Zeile**.
3. Es gibt keinen Gitlink und keine `.gitmodules`.
4. Der Import ist vollständig: Blob-ID und Modus stimmen je Datei mit dem
   Web-TRX-Commit überein; nur die absichtlich geänderten Dateien weichen
   ab.
5. Die Ignorierregeln greifen (`run/`, `web-trx.env`, `.venv`,
   `node_modules`, `dist`).
6. `bash -n` und `shellcheck` sind sauber.
7. Die pluto-tx-Tests zeigen je Test-ID keine Verschlechterung gegenüber
   M1, und alle neuen Tests sind **grün**. Auf dem Radio-Server dürfen die
   neuen Tests nicht übersprungen werden, auch die GUI-Integrationstests
   nicht.
8. `install-web-trx.sh` läuft echt durch. Dazu werden ein venv und das
   Frontend im Scratch-Klon angelegt, der Starter kommt in ein
   Wegwerf-HOME.
9. Web-TRX: `ruff` ist sauber, `pytest` zeigt gegenüber M1 keine
   Regression (die drei umbenannten Tabellentests werden zugeordnet), und
   `npm run check` läuft durch.
10. **Smoke-Test** mit dem sim-Backend auf `127.0.0.1:8322` über HTTP,
    ohne den Produktiv-Server zu berühren. Er prüft:
    - `start`, `status` und `status --json` (mit `devices`),
    - einen zweiten Start (Exit 3),
    - das Access-Log ohne `/health`,
    - `restart` und `stop`,
    - **zwei gleichzeitige Starts**, die genau einen Server ergeben,
    - eine **fremde PID** in der PID-Datei, die nie beendet wird,
    - die Launcher-Befehle.

Erwartet: **„Alle Prüfungen bestanden.“** Jede Abweichung → **HALT**.

Danach die **GUI-Sichtprüfung durch den Betreiber** aus dem Scratch-Klon.
Die Apps bekommen eine Adresse, die es nicht gibt, damit sie kein echtes
Gerät öffnen; der Produktiv-Server ist davon nicht betroffen:

```bash
cd ~/merge-web-trx/work/pluto-tx
printf 'WEB_TRX_BACKEND=sim\nWEB_TRX_HOST=127.0.0.1\nWEB_TRX_PORT=8322\nWEB_TRX_TLS=false\nWEB_TRX_PASSWORD=probe\n' > web-trx/web-trx.env
python3 -m pluto_tx.app --gui --uri ip:192.0.2.1 &
python3 -m pluto_advanced_rx.app --uri ip:192.0.2.1 &
```

In beiden Apps ist Folgendes zu sehen bzw. zu prüfen:
- Die Zeile „Web-TRX: stopped“ mit *Start*.
- *Start*: Die Zeile wechselt über „starting...“ zu „running“.
- *Open*: öffnet `http://localhost:8322`.
- *Stop*: Nach der Rückfrage wechselt die Zeile auf „stopped“.
- Nach Start aus der einen App zeigt die andere innerhalb von 3 s
  ebenfalls „running“.
- Beim Schließen der App läuft der Server weiter (`web-trx/scripts/status.sh`).

Danach aufräumen:

```bash
web-trx/scripts/stop.sh
rm web-trx/web-trx.env
```

**HALT:** Das Verify-Ergebnis berichten
(`results/verify-summary.txt` und die Ausgabe) und die Freigabe für M5
einholen.

### M5 — Übernahme nach `main`

1. Branch hochladen und durchsehen lassen:
   ```bash
   W=~/merge-web-trx/work/pluto-tx
   git -C "$W" push github merge-web-trx
   ```
   Der Betreiber sieht sich den Vergleich auf GitHub an:
   `https://github.com/jochenhammes/pluto-tx/compare/main...merge-web-trx`.
   **HALT** bis zur Freigabe.
2. **Fast-forward** auf `main`, ohne Merge-Commit und ohne Force:
   ```bash
   git -C "$W" fetch github
   git -C "$W" merge-base --is-ancestor github/main merge-web-trx && echo "fast-forward möglich"
   git -C "$W" push github merge-web-trx:main
   ```
   Wird der Push abgelehnt, weil `main` sich bewegt hat → **HALT**. Nie
   forcen. Stattdessen M0 erneut und M3 mit `--fresh`.
3. Den Betreiber bestätigen lassen, dass gerade kein Funkbetrieb läuft und
   kein Browser offen ist. Dann den alten Server stoppen:
   ```bash
   ~/Dokumente/Web-TRX/scripts/stop.sh
   ```
   Ist ein erzwungener Abbruch nötig: den Safe-State des Senders prüfen
   (`iio_attr`), berichten, **HALT**.
4. Laufzeitdaten ein zweites Mal sichern, jetzt konsistent:
   ```bash
   "$T/20_backup.sh" --after-stop
   ```
   Bricht es wegen eines SQLite-Journals ab → **HALT**. Den alten Server
   nicht einfach wieder starten, sondern berichten.
5. Den Produktiv-Checkout nachziehen:
   ```bash
   git -C "$P" pull --ff-only
   ```
   Danach existiert `$P/web-trx/`, und `$P/merge web-trx/` ist weg.

### M6 — Laufzeitdaten umziehen (VOR der Installation), installieren, starten

1. Migration zuerst als Trockenlauf, dann ausführen:
   ```bash
   "$T/50_migrate_runtime.sh"
   ```
   Kopiert werden:
   - `web-trx.env`,
   - `run/settings.json`,
   - `run/sessions.json`,
   - `run/web_trx_tx_log.sqlite3`,
   - `run/tls/{cert,key}.pem`.

   Zeigt der Trockenlauf `*_PATH`-Zeilen, die in den alten Checkout zeigen
   (z. B. `WEB_TRX_PLUTO_TX_PATH`) → **HALT**. Den Betreiber fragen, ob sie
   in der Kopie auskommentiert werden sollen (empfohlen, der Standard ist
   jetzt das Repo selbst). Dann:
   ```bash
   "$T/50_migrate_runtime.sh" --apply --rewrite-paths    # bzw. ohne --rewrite-paths, wenn es keine solchen Zeilen gibt
   ```
   Erwartet: Alle Dateien sind identisch, `key.pem` hat Modus 600, und
   `git status` ist sauber. Bei Konflikten (Zieldatei existiert mit anderem
   Inhalt) → **HALT**. `--force` nur nach Rückfrage.
2. Installieren. Die Constraints sorgen für dieselben Paketstände wie
   vorher, ohne neues numpy:
   ```bash
   cd "$P" && ./install-web-trx.sh --constraints "$(cat ~/merge-web-trx/backup/LATEST)/old-venv-freeze.txt"
   ```
   Sonderfälle:
   - Fehlt Node.js ≥ 18: `--reuse-dist ~/Dokumente/Web-TRX` anhängen. Das
     alte gebaute Frontend wird nur übernommen, wenn die Quellen identisch
     sind.
   - Schlägt der Selbsttest fehl (z. B. numpy im venv) → **HALT**.
3. Starten und prüfen:
   ```bash
   web-trx start && web-trx status && curl -sk https://localhost:8321/health
   ```
   Erwartet:
   - `läuft (PID …), Backend GnuRadioBackend`,
   - in `/health` das Feld `devices`,
   - im Log `web-trx/run/web-trx.log` die Zeilen „restored …“, falls
     vorher Geräte verbunden waren.

### M7 — Abnahme auf der Hardware

Die Checkliste `60_acceptance.md` (in `$T/`) gemeinsam mit dem Betreiber
abarbeiten und die Punkte abhaken.

**HALT vor jedem Senden:** Frequenz, Leistung und Modus nennt der
Betreiber; Test-Sendungen mit niedriger Leistung.

Schlägt ein Punkt fehl: berichten und gemeinsam entscheiden, ob er
behoben oder ob M9 ausgeführt wird.

### M8 — Aufräumen (erst nach bestandener Abnahme)

1. Im alten Repo einen Hinweis setzen und pushen:
   ```bash
   cd ~/Dokumente/Web-TRX
   git pull --ff-only
   { cat "$T/../files/Web-TRX-archive-README.md"; echo; echo '---'; echo; cat README.md; } > README.new && mv README.new README.md
   git commit -am "README: moved into pluto-tx (web-trx/), this repository is archived" && git push
   ```
   Danach archiviert der **Betreiber** das Repo auf GitHub
   (Settings → Archive this repository).
2. Den Branch `merge-web-trx` auf GitHub löschen:
   `git -C ~/merge-web-trx/work/pluto-tx push github --delete merge-web-trx`
3. `~/Dokumente/Web-TRX` bleibt **zwei Wochen** stehen, als Rollback-Basis.
   Danach entscheidet der Betreiber.
4. `~/merge-web-trx/work` darf gelöscht werden. `~/merge-web-trx/backup`
   bleibt, bis der Betreiber es freigibt.
5. Eigene Notizen oder Memory der lokalen Claude-Session anpassen: Web-TRX
   liegt jetzt in pluto-tx unter `web-trx/`, gearbeitet wird auf pluto-tx
   `main`, und das ganze Repo ist frei.

### M9 — Rollback (nur bei Bedarf, nach Rücksprache)

| Situation | Vorgehen |
|---|---|
| Vor M5 (nur Scratch-Klon) | Nichts zurückzunehmen: `~/merge-web-trx/work` löschen, der Tag ist harmlos (`git tag -d pre-web-trx-merge`). |
| M5 gepusht, Server noch nicht umgezogen | Den alten Server wieder starten: `~/Dokumente/Web-TRX/scripts/start.sh`. Der Code auf `main` stört ihn nicht. |
| Nach M6/M7: neuer Server macht Probleme | `"$T/90_rollback.sh" --all` (neuen Server stoppen, dessen Laufzeitdaten zurückkopieren, alten Server starten, Starter `web-trx` beiseitelegen). Jede Aktion fragt nach (`--yes` nach Freigabe). |
| Apps (TX/RX) verhalten sich falsch | Zuerst `PLUTO_WEBTRX_CONTROL=off pluto-tx`, das schaltet die neue Zeile ab. Reicht das nicht: `"$T/90_rollback.sh" --revert-code`. Das erzeugt Revert-Commits auf `main`, lokal, und verschiebt die Reste von `web-trx/` in die Sicherung. Der Push erfolgt nach Rücksprache. |

**Einschränkung:** Der alte Server importiert das lebende
`~/Dokumente/plutosdr`. Der Rollback auf den alten Server funktioniert
daher nur, solange in den zwei Wochen niemand die Schnittstellen von
`pluto_tx`/`pluto_advanced_rx` ändert. Erst danach wieder
Weiterentwicklung wie FT8 F2 dort.

---

## 3. Entscheidungshilfen bei Problemen

| Problem | Was tun |
|---|---|
| Preflight: Repo nicht sauber / nicht aktuell | Betreiber fragen, was mit den Änderungen passieren soll. Nicht selbst committen, stashen oder verwerfen. |
| Preflight: `gui.py`/README seit dem Entwurf geändert | Kein Blocker. M3 versucht den 3-Wege-Merge; bei Konflikt siehe `patches/README.md`. |
| Baseline: Tests rot | Kein Blocker, sie gelten als vorbestehend. Berichten. |
| Verify: `REGRESS` | **Blocker.** Den Test einzeln laufen lassen (`python3 -m unittest -v tests.<modul>.<Klasse>.<test>` im Scratch-Klon mit `PLUTO_WEBTRX_CONTROL=off`), Ursache berichten. Nicht den Test ändern. |
| Verify: `NEW-RED` / neue Tests übersprungen | **Blocker** auf dem Radio-Server (GNU Radio und PyQt5 sind dort da). Ursache berichten. |
| Verify: Port 8322 belegt | `SMOKE_PORT` in `~/merge-web-trx/merge.env` ändern. |
| `install-web-trx.sh`: numpy im venv | Die Constraints-Datei fehlte oder passte nicht. Berichten; nicht `pip install numpy==…` raten. |
| `install-web-trx.sh`: Node fehlt/zu alt | `--reuse-dist ~/Dokumente/Web-TRX` oder Node per nvm (Betreiber). |
| `stop.sh` Exit 2 | Der Server wurde per SIGKILL beendet. Safe-State des Senders prüfen (`iio_attr -u ip:… -c ad9361-phy`: TX-Dämpfung maximal, LO aus). |
| Migration: SQLite-Journal | Die Datenbank wurde nicht sauber geschlossen. Nicht kopieren, berichten. |

---

## 4. Nach dem Merge

- **Arbeitsort:** pluto-tx `main`. Web-TRX liegt in `web-trx/`, das ganze
  Repo ist frei bearbeitbar.
- **Tests vor jedem Commit:**
  - pluto-tx (Repo-Wurzel):
    `QT_QPA_PLATFORM=offscreen PLUTO_WEBTRX_CONTROL=off python3 -m unittest discover tests`
  - Web-TRX:
    `cd web-trx/backend && .venv/bin/python -m pytest && .venv/bin/ruff check web_trx tests`
  - Frontend: `cd web-trx/frontend && npm run check`
- Nie `pytest` aus der Repo-Wurzel starten, denn beide Test-Ordner heißen
  `tests`.
- **FT8** geht mit `web-trx/docs/FT8_PLAN.md` weiter; dessen Regeln sind
  im Merge schon angepasst.

## Anhang: Dateien in diesem Ordner

| Datei | Zweck |
|---|---|
| `README.md` | Kurzüberblick |
| `MERGE_PLAN.md` | dieser Plan |
| `BASE` | Commit-Stände, gegen die die Entwürfe geschrieben und getestet wurden |
| `merge.env.example` | Konfiguration der Skripte (Pfade, Ports) |
| `scripts/lib.sh` | gemeinsame Funktionen |
| `scripts/00_preflight.sh` … `90_rollback.sh` | die Phasen |
| `scripts/unittest_json.py`, `compare_results.py` | Testergebnisse je Test-ID festhalten und vergleichen |
| `scripts/60_acceptance.md` | Abnahme-Checkliste (M7) |
| `files/` | neue Dateien, die an ihren Platz kopiert werden |
| `patches/` | Änderungen an bestehenden Dateien (git-Patches) + Handanleitung |
