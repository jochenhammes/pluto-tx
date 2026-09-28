# merge web-trx — temporärer Ordner

Entwürfe und Skripte, mit denen Web-TRX
([jochenhammes/Web-TRX](https://github.com/jochenhammes/Web-TRX)) als
`web-trx/` in dieses Repository übernommen wird. Web-TRX wird damit das
vierte Programm neben `pluto-tx`, `pluto-advanced-rx` und `pluto-cli`, und
beide Apps bekommen eine Zeile „Web-TRX“ mit Start/Stop/Open.

**Dieser Ordner ändert nichts, solange niemand die Skripte ausführt.** Er
wird vom letzten Merge-Commit wieder gelöscht.

## Ausführen

Lokal auf dem Radio-Server, in einer Claude-Code-Session:

> Lies `~/Dokumente/plutosdr/merge web-trx/MERGE_PLAN.md` und führe ihn
> Phase für Phase aus. Halte an jedem HALT an.

`MERGE_PLAN.md` ist der vollständige Ablauf (M0 Preflight … M7 Abnahme,
M8 Aufräumen, M9 Rollback). Alle Befehle stehen dort.

## Sicherheitsprinzipien

- Gebaut und getestet wird in **Scratch-Klonen** (`~/merge-web-trx/work`).
  Der Produktiv-Checkout bekommt am Ende nur ein `git pull --ff-only`.
- **Kein Force-Push, kein Reset.** Der Rollback läuft per `git revert` und
  über den alten Checkout, der zwei Wochen stehen bleibt.
- Laufzeitdaten (Passwort, Anmeldungen, TX-Log, Einstellungen, Zertifikat)
  werden **kopiert**, mit Prüfsumme, nie verschoben. Vorher kommen zwei
  Sicherungen (Git-Bundles und tar).
- Die GUI-Änderungen an beiden `gui.py` sind **reine Einfügungen**. Die
  neue Zeile kann die App nicht am Start hindern, fragt erst nach dem
  ersten Anzeigen des Fensters etwas ab und öffnet Dialoge nur nach einem
  Klick.
- Tests je Test-ID vor und nach dem Merge: Nichts darf schlechter werden.
- Zwischen Web-TRX und den Apps gibt es drei Schutzstellen gegen
  gleichzeitigen Zugriff auf dasselbe SDR.

## Inhalt

```
MERGE_PLAN.md          ausführbarer Plan (M0–M9), Entscheidungshilfen
BASE                   Stände, gegen die alles geschrieben und getestet wurde
merge.env.example      Pfade/Ports der Skripte (Standardwerte passen normalerweise)
scripts/               00_preflight … 90_rollback, lib.sh, Testergebnis-Vergleich, 60_acceptance.md
files/                 neue Dateien (install-web-trx.sh, pluto_tx/webtrx_*.py, Tests,
                       web-trx/scripts/*.sh, pluto_path.py, CI-Workflow, Archiv-README)
patches/               Änderungen an bestehenden Dateien + Handanleitung (README.md)
```

## Wie die Entwürfe geprüft wurden

In einer Cloud-Session ohne GNU Radio und ohne Hardware lief der komplette
Ablauf M0–M6 samt Rollback einmal als normaler Benutzer durch. Dafür gab
es nachgebaute Checkouts und einen alten Web-TRX im sim-Modus mit Login,
Sessions, TX-Log und Zertifikat.

Geprüft wurden:
- 36 Controller-, Skript- und Widget-Tests, grün,
- die Web-TRX-Tests: 107 grün, dazu 1 übersprungener, der GNU Radio braucht,
- ruff, shellcheck und `npm run check`,
- der Smoke-Test, bestanden,
- die Migration (auch ein zweiter Lauf) und der Rollback bis zurück zum
  exakten Vor-Merge-Stand.

Nicht geprüft werden konnten dort: die GUI-Integrationstests (brauchen GNU
Radio), die pluto-tx-Testsuite und alles mit Hardware. Das erledigen M4
und M7 auf dem Radio-Server.
