# Debug-/Teststrategie

Zwei `SessionBackend`-Implementierungen (`backend/web_trx/session.py`)
tragen den gesamten Stack, je nachdem, was gerade verfügbar ist:

## 1. `SimBackend` — überall lauffähig, kein GNU Radio/keine Hardware nötig

`backend/web_trx/sim_backend.py`: reines Python/NumPy, erzeugt plausibel
aussehende (aber **nicht physikalisch korrekte**) Spektren und dasselbe
Event-Vokabular, das der echte Flowgraph später auch emittiert (`keyed`,
`pocsag_message`, `estop`, …). Damit lässt sich entwickeln/testen, ohne
je an einen SDR heranzukommen:

- **Unit-Tests** direkt gegen `SimBackend` (`backend/tests/test_sim_backend.py`)
  — Zustandsautomat, Safety/E-STOP, Moduswechsel-Validierung.
- **Protokoll-Tests** über die echte FastAPI-WebSocket-Schicht
  (`backend/tests/test_server_ws.py`), inkl. Binärkanal (Spektrum-Zeilen).
- **Frontend-Tests im echten Browser** gegen einen laufenden
  `uvicorn`-Prozess mit `SimBackend` — Chromium ist in dieser
  Cloud-Umgebung vorinstalliert (`/opt/pw-browsers/chromium`), ansteuerbar
  z. B. über `playwright-core` mit `executablePath` darauf gesetzt.

```bash
cd backend && . .venv/bin/activate && python -m pytest
uvicorn web_trx.server:create_app --factory --port 8321  # Terminal 1
cd frontend && npm run dev                       # Terminal 2, Proxy auf :8321
```

Alles oben Genannte läuft in jeder Umgebung ohne GNU Radio/libiio/SDR —
inklusive dieser Cloud-Session selbst (hier sind beide nicht installiert,
siehe unten).

## 2. `GnuRadioBackend` — nur auf einem System mit GNU Radio + Hardware

Verdrahtet `PlutoTxFlowgraph`/`AdvancedRxFlowgraph`/`FftProbe`/
`PlutoSafety` aus pluto-tx (Repo-Wurzel, eine Ebene über `web-trx/`),
nach dem Muster von `pluto_cli/runtime.py`. Läuft nur dort, wo
`install.sh` bereits gelaufen ist (echter Radio-Server).
Bewusst so dünn wie möglich gehalten — die eigentliche Komplexität
(Protokoll, Zustandsautomat, UI, Audio-Framing) ist bereits gegen
`SimBackend` durchgetestet, bevor sie auf echte Hardware trifft.

Stand: `backend/web_trx/radio_backend.py` existiert und ist auf dem
Radio-Server gegen einen Pluto+ (`ip:plutoplus.local`) verifiziert (RX:
Wasserfall, Audio, Moduswechsel). Die Tests in
`backend/tests/test_radio_backend.py` überspringen sich selbst, wo GNU
Radio fehlt; der Hardware-Test braucht zusätzlich `WEB_TRX_HW_TESTS=1`.

## Hardware-in-the-Loop

Für den finalen Test gegen echte Hardware: eigene Claude-Code-Session auf
dem Radio-Server (mit GNU Radio + Hardware-Zugriff), einbindbar per
`SendMessage`, sobald sie eingerichtet ist. Näheres siehe Absprache in der
Projekt-Konversation; wird relevant ab Meilenstein M2/M3
(Wasserfall/Audio, siehe `docs/PROJECT_PLAN.md`).

## Bekannte Stolpersteine (bereits gefunden & gefixt)

**Self-Cancellation-Deadlock.** `SimBackend`s POCSAG-Auto-Unkey-Task rief
am Ende `ptt(False)` auf, was wiederum versuchte, genau diese (sich
selbst gerade ausführende) Task zu canceln — die `unkeyed`-Event-
Auslieferung brach dadurch mitten im `await` ab, das Backend hing
scheinbar (siehe Kommentar in `sim_backend.py`s `_auto_unkey_after()`).
Gefunden über einen `faulthandler`-Traceback-Dump auf einen künstlichen
Timeout, nicht durch Raten — bei ähnlichen "hängt ohne Fehlermeldung"-
Fällen mit Fire-and-forget-`asyncio.create_task`s ist das der schnellste
Weg zur Ursache.

**Seiteneffekte beim reinen Import.** `server.py` hatte lange ein
Modul-Level `app = create_app()` (üblich, damit `uvicorn
web_trx.server:app` die App findet). Das führte dazu, dass jeder Import
des Moduls — auch nur für `from web_trx.server import create_app` in
Tests, auch nur beim Pytest-Collection-Schritt — eine komplette
zusätzliche Default-App baute: TX-Log-SQLite-Datei wurde ins
Arbeitsverzeichnis geschrieben, ein zufälliges Passwort generiert und auf
stderr ausgegeben. Gefunden, weil ein Test unerwartet die
Passwort-Generierungsmeldung in `capsys`-Output zeigte, obwohl der Test
explizit ein eigenes `AuthManager(password=...)` übergab. Fix: kein
Modul-Level `app` mehr, stattdessen `uvicorn web_trx.server:create_app
--factory` (siehe README). Allgemeine Lehre: ein Modul, das auch als
Bibliothek importiert wird (hier: für `create_app`), darf beim bloßen
Import keine Seiteneffekte (Dateien schreiben, Netzwerk, Zufallswerte
ausgeben) auslösen.

**Garbage-collected GNU-Radio-Python-Block.** Ein eigener
`gr.sync_block` (der Audio-Abgriff `_AudioTap` in `radio_backend.py`), der
nur per `tb.connect()` in einen Flowgraph gehängt wird, hat danach keine
Python-Referenz mehr — `connect()` hält keine. Sobald er eingesammelt
wird, stirbt der Scheduler-Thread mit `terminate reached ... AttributeError:
'array_like' object has no attribute 'stop'` (der Speicher des Blocks
gehört inzwischen einem anderen Objekt). Isoliert (Block als globale
Variable) lief er fehlerfrei, das hat die Ursache eingegrenzt. Fix: Referenz
am Flowgraph ablegen (`tb.web_trx_audio_tap`).

**M17 tastete nicht auf („tastet auf…“ blieb stehen).** Das Rufzeichen-Feld
schickt seinen Wert erst bei `change` (Fokusverlust); der PTT-Knopf nimmt
per `pointerdown|preventDefault` absichtlich keinen Fokus — Rufzeichen
eintippen und direkt PTT drücken hieß also: der Server bekam nie ein
Rufzeichen und lehnte das Auftasten ab, das Frontend ignorierte den
Fehler. Fix: vor `ptt_on` werden geänderte Modus-Parameter nachgeschickt,
Ablehnungen setzen den PTT-Zustand zurück und werden im TX-Panel angezeigt.

**Mikrofon-Audio schneller als Echtzeit.** Ein Durchgang lieferte 4,46 s
Audio in 3,54 s Wanduhrzeit: Chrome stellt `ScriptProcessorNode`-Puffer,
die während einer Hauptthread-Blockade aufgelaufen sind, gesammelt zu —
auch solche von *vor* dem PTT-Druck. Fix: jeder Block trägt seine Position
auf der AudioContext-Uhr, gesendet wird nur, was nach dem PTT-Druck
aufgenommen wurde. Gefunden über die Pro-Durchgang-Statistik
(`tx over done` im Log).

**Log-Filter in `start.sh` hat den Server getötet.** Die RADE-Zeilen werden
per Process-Substitution (`> >(grep -v ...)`) herausgefiltert. Ein
`</dev/null` war dabei versehentlich *in* die Substitution gerutscht —
`grep` las /dev/null statt der Server-Ausgabe, beendete sich sofort, und
uvicorn starb beim nächsten Schreiben an SIGPIPE, ohne jede Log-Zeile.
Sichtbar erst mit `bash -x scripts/start.sh`.

**AudioWorklet-Module als `data:`-URL.** Vite bettet kleine Assets (auch per
`?url` importierte `.js`-Dateien) als `data:`-URL ein. Für
`audioWorklet.addModule()` ist das unzuverlässig. Deshalb schließt
`vite.config.ts` (`assetsInlineLimit`) alle `*.worklet.*`-Dateien davon aus,
im Build liegen sie als eigene Dateien unter `dist/assets/`.

**Leerer Verbindungs-String ist gültig.** Für RTL-SDR/HackRF heißt `""`
„automatisch“. Die Wiederverbinden-Logik prüfte zuerst auf einen
nicht-leeren String und stellte deshalb den RTL-SDR-Empfänger nach einem
Neustart nicht wieder her. Gespeichert wird jetzt ein eigenes
`connected`-Flag.

**RTTY dekodierte nur Zeichensalat (Geräteablage, kein Softwarefehler).**
Pluto (TX) und RTL-SDR (RX) lagen bei 145 MHz ~58 Hz auseinander
(≈0,4 ppm). Die RADE-Statusanzeige hatte diesen Wert direkt gemessen. Bei
170 Hz Shift reicht das für falsche Bits, und die RTTY-AFC korrigiert
Ablagen unter `RTTY_AFC_DEADBAND_HZ` (60 Hz) absichtlich nicht. Nachweis:
Mit der RX-Mark um −58 Hz verschoben kam der Text fehlerfrei an. Lösung:
Frequenzkorrektur in ppm je Richtung (`freq_correction_ppm`).

**RTL-SDR wurde nach Neuladen der Seite als „nicht verbunden“ angezeigt.**
Das Frontend prüfte `!!rx.connection`, bei RTL-SDR/HackRF ist die Verbindung
aber `""` (automatisch). Nur `null` bedeutet „nicht verbunden“; dieselbe
Falle wie beim Wiederverbinden nach einem Neustart (oben).

**FT8: leere Slots direkt nach dem Serverstart.** Beim ersten Messlauf
lieferte Web-TRX drei Slots lang 0 Decodes. Vorausgegangen waren mehrere
Umbauten direkt nacheinander: Wiederherstellen der RX-Verbindung nach dem
Start, Direct Sampling einschalten (Umbau), Frequenz, FT8 wählen (Umbau).
Nach einem einzelnen Moduswechsel fehlt dagegen nur der angebrochene Slot,
wie bei pluto-cli; im Dauerbetrieb dekodiert Web-TRX gleich viel. Nicht
weiter untersucht; falls es wieder auffällt: prüfen, ob der RTL-SDR nach
dem Umschalten auf Direct Sampling einige Sekunden braucht.

**FT8-Serie abbrechen und dabei die TX-Sperre halten = Deadlock.** Die
Serie tastet unter `_tx_lock` auf und ab; ein Abbruch, der auf ihr Ende
wartet (`Ft8Series.cancel()` ohne `device_safe`), darf deshalb nie
aufgerufen werden, während der Aufrufer die Sperre hält. `disconnect()`
bricht die Serie darum vor dem `async with self._tx_lock` ab; innerhalb der
Sperre (`_teardown_tx`) nur mit `device_safe=True`, das nicht wartet.
Außerdem: `key_ptt()` läuft im Worker-Thread und lässt sich nicht abbrechen.
Ein `task.cancel()` während des Auftastens würde die Serie beenden, während
der Thread den Sender trotzdem auftastet. `cancel()` wartet in dem Fall auf
das Ende von `key()` und tastet sofort ab, und `key()` prüft nach dem Warten
auf die Sperre `series.cancelled`, bevor es auftastet.

**`pgrep -f`/`pkill -f` mit einem Muster aus der eigenen Kommandozeile**
trifft auch die Shell, die den Befehl ausführt (Exit-Code 144). Web-TRX
deshalb über `scripts/stop.sh` beenden, Testserver über die PID aus `$!`.

**Eine gemeinsame TX-Leistung für alle Geräte wäre gefährlich.** Beim HackRF
ist die Leistung eine ZF-Verstärkung (20 = 20 dB), beim Pluto eine
Dämpfung. Derselbe gespeicherte Wert 20 wäre beim Pluto auf 0 dB geklemmt
worden, also volle Leistung. Aufgefallen beim Wechsel HackRF → Pluto im
Test (ohne Aussendung). Seitdem steht die Leistung je Gerät in
`power_by_device`; ein Gerät ohne gespeicherten Wert startet mit seiner
sicheren Voreinstellung.
