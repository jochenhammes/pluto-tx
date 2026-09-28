# Patches

`30_build_merge.sh` wendet die Patches mit `git apply --index` an. Klappt
das nicht, weil sich eine Datei seit dem Stand in `../BASE` geändert hat,
versucht das Skript `git apply --3way`. Bleiben Konflikte, bricht es ab.
Dann gilt diese Anleitung: die Änderung **von Hand** einarbeiten, und zwar
in einer eigenen Kopie der Entwürfe, nicht im Scratch-Klon:

1. Die Entwürfe kopieren: `cp -a ~/merge-web-trx/tools ~/merge-web-trx/tools-neu`.
2. Einen frischen Klon anlegen und die Datei dort auf den neuen Stand
   bringen, wie unten beschrieben.
3. Den Patch aus dem Klon neu erzeugen:
   `git diff -- <dateien> > ~/merge-web-trx/tools-neu/patches/<name>.patch`.
4. Den Betreiber das Ergebnis ansehen lassen, dann
   `~/merge-web-trx/tools-neu/scripts/30_build_merge.sh --fresh`.

| Patch | Dateien | Inhalt |
|---|---|---|
| `03-web-trx-backend.patch` | `web-trx/backend/web_trx/server.py`, `radio_backend.py`, `tests/test_modes.py`, `tests/test_server_ws.py` | `/health` mit `devices` (nur Loopback, laufender Restore zählt als belegt), Access-Log-Filter, Tabellentests gegen die Repo-Wurzel (`parents[3]`), neue Tests |
| `06-gui-integration.patch` | `pluto_tx/gui.py`, `pluto_advanced_rx/gui.py` | Web-TRX-Zeile, Connect-Guard, Startup-Prüfung — **nur Einfügungen** |
| `07-docs.patch` | `README.md`, `web-trx/README.md`, `web-trx/docs/*.md` | vier Programme, Installation/Betrieb von web-trx, FT8-Plan-Regeln, kein Submodule |
| `08-comments.patch` | Kommentare in `web-trx/backend/web_trx/*.py`, `frontend/src/App.svelte`, `pyproject.toml` | `vendor/pluto-tx` → pluto-tx im selben Repo |

`pluto_path.py` wird nicht gepatcht, sondern aus `../files/` kopiert,
ebenso alle anderen neuen Dateien.

## GUI-Einbau von Hand (06)

Nur einfügen, nichts löschen und nichts umformulieren. `40_verify.sh`
lehnt jede gelöschte Zeile in den beiden `gui.py` ab. Die Stellen werden
über **Ankertexte** gefunden, nicht über Zeilennummern.

### TX (`pluto_tx/gui.py`)

1. **Zeile.** Direkt nach dem Anker
   `        section1.addLayout(self.aioc_serial_row)`
   einfügen:
   ```python

           # --- Web-TRX row: state of the browser UI's server + Start/Stop/Open
           # (pluto_tx/webtrx_widget.py). Built defensively: whatever goes wrong
           # here, the app itself still starts -- the row then only says why.
           try:
               from .webtrx_widget import WebTrxRow
               self.webtrx_row = WebTrxRow(
                   has_device=lambda: self.tb is not None,
                   release_device=lambda: self._disconnect() if self.tb is not None else None,
                   device_type=lambda: self.device_type_combo.currentData(),
                   show_message=lambda text: self.status_label.setText(text),
               )
               section1.addWidget(self.webtrx_row)
           except Exception as e:
               self.webtrx_row = None
               section1.addWidget(QtWidgets.QLabel(f"Web-TRX: not available ({e})"))
   ```
2. **Connect-Guard.** Als erste Zeilen von `def _on_connect_clicked(self):`
   einfügen:
   ```python
           # Web-TRX guard: ask before opening a device type the Web-TRX server
           # is known to hold (pluto_tx/webtrx_widget.py confirm_connect()).
           if self.tb is None and getattr(self, "webtrx_row", None) is not None \
                   and not self.webtrx_row.confirm_connect(self.device_type_combo.currentData()):
               return
   ```
3. **Startflag.** Um den automatischen Verbindungsaufbau am Ende von
   `__init__` herum, also um den Anker
   `        self._rebuild(uri, self._wav_path)`, der direkt auf
   `self.mode_tab_widget.currentChanged.connect(self._on_mode_tab_changed)`
   folgt:
   - davor: `        self._webtrx_startup = True  # see the Web-TRX check at the top of _rebuild()`
   - danach: `        self._webtrx_startup = False`
4. **Startup-Prüfung.** In `def _rebuild(self, connection, wav_path):`
   direkt nach dem Docstring, vor `self._repeat_cancel()`, einfügen:
   ```python
           # Automatic connect at app start while the Web-TRX server holds this
           # device type: stay disconnected instead (pluto_tx/webtrx_widget.py
           # startup_blocks(); _webtrx_startup is only set around that one call).
           if getattr(self, "_webtrx_startup", False) and getattr(self, "webtrx_row", None) is not None \
                   and self.webtrx_row.startup_blocks(self.device_type_combo.currentData()):
               self.status_label.setText(self.webtrx_row.startup_message(self.device_type_combo.currentData()))
               self._set_connected_controls_enabled(False)
               self.connect_button.setText("Connect")
               self.uri_combo.setEnabled(True)
               self.aioc_serial_combo.setEnabled(True)
               self.device_type_combo.setEnabled(True)
               return
   ```
   Die Zeilen vor `return` entsprechen genau dem Zweig
   `if probe_error is not None:` weiter unten in `_rebuild`. Hat sich
   dieser geändert, hier dieselben Zeilen verwenden.

### RX (`pluto_advanced_rx/gui.py`)

1. **Zeile.** Direkt nach dem Anker
   `        device_row.addWidget(self.connect_button)`, also vor
   `left_column.addWidget(device_group)`, denselben Block wie bei TX
   einfügen, mit zwei Unterschieden:
   - `from pluto_tx.webtrx_widget import WebTrxRow` statt `from .webtrx_widget …`,
   - `device_group_layout.addWidget(...)` statt `section1.addWidget(...)`,
     an beiden Stellen.
2. **Connect-Guard.** Derselbe Block wie bei TX, als erste Zeilen von
   `def _on_connect_clicked(self):`.
3. **Startflag.** Um `        self._connect(uri)` am Ende von `__init__`,
   das direkt auf `self.device_type_combo.setEnabled(True)` folgt:
   - davor: `        self._webtrx_startup = True  # see the Web-TRX check at the top of _connect()`
   - danach: `        self._webtrx_startup = False`
4. **Startup-Prüfung.** Als erste Zeilen von `def _connect(self, uri_text):`
   einfügen. Das Fenster ist dort schon im getrennten Zustand, daher nur
   die Statuszeile:
   ```python
           # Automatic connect at app start while the Web-TRX server holds this
           # device type: stay disconnected instead (pluto_tx/webtrx_widget.py
           # startup_blocks(); _webtrx_startup is only set around that one call).
           if getattr(self, "_webtrx_startup", False) and getattr(self, "webtrx_row", None) is not None \
                   and self.webtrx_row.startup_blocks(self.device_type_combo.currentData()):
               self.status_label.setText(self.webtrx_row.startup_message(self.device_type_combo.currentData()))
               return
   ```

Kontrolle danach:
`tests/test_webtrx_gui_integration.py` muss grün sein. Er prüft die
Zeile, den kaputten Import, den Connect-Guard, die Startup-Prüfung und die
abgeschaltete Integration.

## Backend von Hand (03)

- `server.py`:
  - Imports `ipaddress`, `logging` und `Request`.
  - `_HealthAccessFilter` und `_install_health_access_filter()`; Letzteres
    wird in `create_app()` vor `FastAPI(...)` aufgerufen.
  - `_is_loopback()` und `device_summary(backend)`.
  - Der `/health`-Handler bekommt `request: Request` und hängt
    `devices = device_summary(manager.backend)` an, aber nur, wenn
    `_is_loopback(request.client.host)` zutrifft.
- `radio_backend.py`:
  - `self._restoring: set[str]` in `__init__`.
  - In `_restore_connections()`: `self._restoring = set(restore)`; im
    `try` je Richtung ein `finally: self._restoring.discard(direction)`.
  - Neue Methode `restoring()` liefert `set(self._restore) | self._restoring`.
- `tests/test_modes.py`:
  - `PLUTO_TX_CONFIG = Path(__file__).resolve().parents[3] / "pluto_tx" / "config.py"`.
  - Skip-Grund: „not inside a pluto-tx checkout“.
  - Die drei Tests `…_match_pinned_pluto_tx` heißen `…_match_pluto_tx`.
    `40_verify.sh` ordnet diese Umbenennung beim Vergleich zu.
- `tests/test_server_ws.py`:
  - `make_client(client_addr=None)`.
  - Vier neue `test_health_*`-Tests.
