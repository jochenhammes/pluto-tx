# Web-TRX — Projektplan

> **Stand seit der Zusammenführung:** Web-TRX liegt als Ordner `web-trx/`
> im pluto-tx-Repository und importiert pluto-tx direkt aus diesem Checkout.
> Das Submodule `vendor/pluto-tx` gibt es nicht mehr; Stellen unten, die es
> beschreiben (Abschnitt 3 und 9), dokumentieren den Weg dorthin.

Einheitliche Weboberfläche für `pluto-tx`/`pluto-advanced-rx` (Sende- und
Empfangs-App für ADALM-PLUTO, HackRF One und RTL-SDR). Ziel: ein Funkamateur
bedient die am Server angeschlossenen SDRs vollständig über den Browser —
Senden und Empfangen, inklusive Wasserfall/Spektrum, Audio und Digimodes.

## 1. Rahmenbedingungen (Setting)

- Ein Server, ein oder mehrere SDRs (Pluto/Pluto+, HackRF One, RTL-SDR,
  Soundkarte/AIOC als TX-Backend für externe Funkgeräte).
- **Immer genau ein Nutzer** — kein Multi-Tenant-Betrieb, kein
  Rechte-/Rollenmodell nötig. Vereinfacht die Server-Architektur erheblich
  (ein globaler Session-State, keine Scheduling-Logik für konkurrierende
  Zugriffe).
- Nutzer ist immer lizenzierter Funkamateur — Verantwortung für
  Frequenzwahl/Bandplan/Leistung bleibt beim Betreiber, wie im bestehenden
  `pluto-tx` auch.
- Modernes Web-Frontend + Backend auf dem Server; Zugriff übers Netzwerk,
  ggf. Internet mit VPN.
- **Backend soll möglichst wenig neu entwickeln** — die eigentliche
  Signalverarbeitung (Flowgraphs, Geräteabstraktion, Sicherheitsschicht)
  kommt aus `pluto-tx`/`pluto-advanced-rx`/`pluto-cli`.
- RX-Kernstück: flüssiger Wasserfall + Spektrum mit einfachen Tune-/Zoom-/
  Fenster-Werkzeugen, analog zu SDR++ (das ebenfalls auf Webtechnologien
  setzt).

## 2. Wiederverwendbare Bausteine aus `pluto-tx` (Bestandsaufnahme)

`pluto-tx` ist bereits fast genau das, was das Web-TRX-Backend braucht, nur
mit Qt-GUI bzw. stdout-JSON statt Web-API als Frontend:

| Baustein | Datei | Wiederverwendung |
|---|---|---|
| `PlutoTxFlowgraph` | `pluto_tx/flowgraph.py` | komplette TX-Signalkette, alle Modi (`MODE_FM`, `MODE_SSB`, `MODE_M17`, `MODE_POCSAG`, …), direkt importierbar |
| `AdvancedRxFlowgraph` | `pluto_advanced_rx/flowgraph.py` | komplette RX-Signalkette inkl. Digimode-Decoder |
| `PlutoSafety` | `pluto_tx/safety.py` | GNU-Radio-unabhängige Abschaltschicht (`force_safe_state()`), NOTAUS-Grundlage |
| Geräteabstraktion | `pluto_tx/devices/*`, `pluto_advanced_rx/devices/*` | Pluto/HackRF/RTL-SDR/Soundcard/AIOC einheitlich, inkl. Scan/Probe |
| `FftProbe` | `pluto_advanced_rx/fft_probe.py` | **Der Schlüsselbaustein für den Web-Wasserfall.** Reiner Python/NumPy-`gr.sync_block`, berechnet FFT-Zeilen laufend und stellt sie pollbar bereit (`get_latest_row(generation)`), inkl. Zoom-FFT und Video-Averaging, alles ohne Flowgraph-Rebuild. Muss nur noch statt an einen Qt-Timer an eine WebSocket-Push-Schleife gehängt werden. |
| JSON-Event-Schema | `pluto_cli/runtime.py`, `pluto_cli/README.md §6` | fertiges, dokumentiertes Ereignisformat (`keyed`, `m17_fields`, `pocsag_message`, `meshtastic_frame`, `filebroadcast_progress`, `error`, …) — Vorlage für das WebSocket-Protokoll, keine Neuerfindung nötig |
| Signal-/Shutdown-Handling | `pluto_cli/runtime.py` (`install_safety_handlers`, `run_tx_session`, `run_rx_session`) | Muster für sichere Zustandsübergänge (SIGINT/SIGTERM/Exception → `shutdown_safe()`), 1:1 auf einen Server-Prozess übertragbar |
| Bandplan/PHY-Konstanten | `pluto_tx/config.py`, `pluto_advanced_rx/config.py` | Bandpläne, M17-/LoRa-PHY-Parameter, Presets |

**Echte Lücken**, die es in `pluto-tx` nicht gibt und die Web-TRX neu bauen
muss:

1. **Netzwerk-Streaming von Spektrum/Wasserfall.** `pluto-cli` sagt es
   explizit: *"No live waterfall/spectrum display. headless by design"*.
   `FftProbe` existiert nur GUI-seitig, gepollt von einem Qt-Timer.
2. **Netzwerk-Audio (bidirektional).** GUI-Apps nutzen lokale ALSA/PipeWire-
   Geräte. Mikrofon-Erfassung im Browser → Server (TX) und Demod-Audio
   Server → Browser (RX) existieren nirgends.
3. **Das eigentliche Web-Frontend** — es gibt aktuell nur Qt-GUI und CLI.
4. **Eine Dauerprozess-Session-Verwaltung** statt CLI-Einzelaufrufen (Connect/
   Disconnect, Moduswechsel, Tuning — alles muss jetzt interaktiv über eine
   laufende Verbindung laufen statt per Kommandozeilen-Flag beim Start).

### Architekturentscheidung: Wie bindet Web-TRX den `pluto-tx`-Code ein?

`pluto-cli` selbst ist die Blaupause: *"a separate top-level package that
imports and reuses `pluto_tx`'s/`pluto_advanced_rx`'s existing flowgraph/
device/audio code directly"*. Web-TRX' Backend folgt demselben Muster —
**es importiert `PlutoTxFlowgraph`/`AdvancedRxFlowgraph`/`FftProbe`/
`PlutoSafety` als Python-Bibliothek**, statt nur `pluto-cli` als
Subprozess zu kapseln. Grund: Nur der direkte Import gibt Zugriff auf
`FftProbe.get_latest_row()` (Wasserfall) und auf die rohen Audio-Sinks/
-Quellen (Netzwerk-Audio) — beides sitzt unterhalb dessen, was `pluto-cli`
als Kommandozeilen-Oberfläche exponiert.

Konkret: Web-TRX liegt als `web-trx/` im pluto-tx-Repository und
importiert `pluto_tx`/`pluto_advanced_rx` aus der Repo-Wurzel
(`backend/web_trx/pluto_path.py`). Bis zur Zusammenführung war pluto-tx
ein fest gepinntes Git-Submodule unter `vendor/pluto-tx`. `pluto-cli` selbst bleibt
zusätzlich nutzbar für einfache One-Shot-Admin-Aufgaben (z. B. Scan-Skripte),
ist aber nicht der Hauptpfad der Laufzeit-Kommunikation.

Lizenz-Hinweis: `pluto-tx` ist GPLv3 — Web-TRX' Backend (das es importiert)
muss GPLv3-kompatibel bleiben.

## 3. Zielarchitektur

```
Browser (SPA)                         Server
┌─────────────────────────┐           ┌──────────────────────────────────────┐
│ Wasserfall/Spektrum      │  WS bin   │  FastAPI/Uvicorn                     │
│ (WebGL Canvas)           │◄─────────►│  ├─ /ws  (control JSON + binary)     │
│ Audio (Web Audio API)    │  WS bin   │  │   ├─ Control-Handler (Connect,    │
│ Steuerpanels pro Modus   │  WS text  │  │   │   Scan, Mode-Select, PTT,     │
│ Login/Session            │  REST     │  │   │   Tune, Params)              │
└─────────────────────────┘           │  │   ├─ Spectrum-Bridge (FftProbe)   │
                                       │  │   └─ Audio-Bridge (RX→WS, WS→TX)  │
                                       │  ├─ Session-State-Machine            │
                                       │  │   (genau 1 aktive TX- ODER        │
                                       │  │    RX-Session je Gerät)           │
                                       │  ├─ Auth (Shared-Token/Passwort)     │
                                       │  ├─ TX-Aktivitätslog (SQLite/JSONL)  │
                                       │  └─ pluto-tx (Repo-Wurzel,           │
                                       │      importiert wie pluto_cli)       │
                                       │        ├─ PlutoTxFlowgraph           │
                                       │        ├─ AdvancedRxFlowgraph        │
                                       │        ├─ PlutoSafety                │
                                       │        └─ FftProbe                   │
                                       └──────────────────────────────────────┘
                                                      │ libiio / SoapySDR / USB
                                                      ▼
                                          Pluto(+) / HackRF / RTL-SDR / AIOC
```

**Protokoll:** eine WebSocket-Verbindung pro Browser-Session.
- Text-Frames: JSON, Erweiterung des bestehenden `pluto-cli`-Event-Schemas
  um Steuerkommandos (`connect`, `scan`, `select_mode`, `tune`, `set_gain`,
  `ptt_on/off`, `set_power_ceiling`, `estop`, …) und deren Antworten/Events.
- Binär-Frames: 1-Byte-Typkennung + Payload — `0x01` Spektrum-/Wasserfall-
  Zeile (Float32Array dB-Werte, aus `FftProbe`), `0x02` Audio-Paket
  (Richtung RX→Browser), `0x03` Audio-Paket (Richtung Browser→TX, Mic).

**Audio-Codec:** Start mit PCM16 über WS (einfach, verifizierbar), Wechsel
auf Opus (WebCodecs/AudioWorklet-Encoder) sobald Bandbreite über VPN/
Internet ein Thema wird — bei Sprachmodi (FM/SSB/M17) mit potenziell
schwacher Anbindung lieber früh einplanen als spät nachrüsten.

**Backend-Threading:** GNU-Radio-Flowgraphs sind nicht async-nativ. Muster:
Flowgraph-Steuerung läuft in einem dedizierten Worker-Thread (analog zu
`pluto_cli/runtime.py`s Tick-Loop, nur dass `time.sleep()`-Ticks durch eine
Thread-sichere Queue ersetzt werden, die der asyncio-Eventloop des
WebSocket-Servers konsumiert). `FftProbe`s bestehendes Lock-/Generation-
Muster passt dafür unverändert.

**Auth:** kein Multi-User-Modell nötig — ein gemeinsames Server-Passwort/
Token reicht, da immer nur ein lizenzierter Betreiber zugreift. Trotzdem
sinnvoll: TLS/WSS-Terminierung per Reverse-Proxy (nginx/Caddy), und beim
Betrieb über Internet dringend VPN (WireGuard) statt offener Exposition —
hier geht es um eine Sendeanlage, nicht nur um Daten.

**TX-Aktivitätslog:** jede Aussendung (Zeit, Modus, Frequenz, Parameter,
Rufzeichen wo vorhanden) persistent protokollieren — sinnvoll für
Nachvollziehbarkeit einer ferngesteuerten Station, baut direkt auf den
ohnehin vorhandenen `keyed`/`unkeyed`/`pocsag_message`/`meshtastic_frame`-
Events auf.

## 4. Betriebsarten-Analyse für die Minimalversion

Pflicht laut Vorgabe: **FM, SSB, M17, POCSAG**. Analyse, warum genau diese
vier eine gute, bewusst kleine MVP-Grenze sind (nicht additiv "irgendwas
draufpacken"):

| Modus | Backend fertig? | Zusatz-Install | Braucht Audio-Stream | UI-Komplexität | Regulatorik |
|---|---|---|---|---|---|
| **FM** | ✅ ja | – | ja (bidirektional) | mittel (CTCSS/DCS-Feld) | niedrig |
| **SSB (USB/LSB)** | ✅ ja | – | ja (bidirektional) | niedrig (Sideband-Wahl) | niedrig |
| **M17** | ✅ ja | `install-m17.sh` | ja (bidirektional) + Metadaten (`m17_fields`) | mittel (Rufzeichenfelder, LSF-Anzeige) | niedrig |
| **POCSAG** | ✅ ja | – | **nein** (reines Text/Daten-Digimode) | niedrig (RIC/Text-Formular, Tabelle) | mittel (nur Amateurfunk-Bänder) |
| FreeDV 2020/2020B | ✅ ja | – (transitiv mit gnuradio) | ja | mittel | niedrig |
| RADE | ✅ ja | `install-rade.sh` | ja | mittel | niedrig |
| PSK31 / RTTY | ✅ ja | – | nein (Text) | niedrig | niedrig |
| Waterfall Writer | ✅ ja | – | nein, aber Wasserfall-Bild-Rendering | hoch (eigene Visualisierung) | niedrig |
| File Broadcast | ✅ ja | – | nein, Datei-Up-/Download | mittel | niedrig |
| Meshtastic / MeshCore | ✅ ja | `install-lora.sh` (+pip) | nein | hoch (Identität, Verschlüsselung, Kanäle, Bandplan-Warnhinweise) | hoch |
| Baseband (fldigi-Bridge) | ✅ ja | – | ja (roh) | niedrig | niedrig |

**Warum genau FM/SSB/M17/POCSAG als V1-Grenze — jenseits der reinen
Vorgabe:** Die vier zusammen zwingen dazu, alle drei fundamentalen
Datenpfade der Plattform einmal komplett durchzubauen, bevor die Breite
wächst:

1. **POCSAG zuerst als "Walking Skeleton"** — kein Audio, nur Text/Daten.
   Beweist Steuerkanal, Wasserfall-Pipeline, Session-State-Machine, Safety/
   E-STOP und das komplette Frontend-Grundgerüst, ohne dass gleichzeitig
   die schwierigste Baustelle (Audio-Streaming) mit hineinspielt. Kleinste
   sinnvolle Ende-zu-Ende-Strecke (RX-Tabelle + TX-Formular).
2. **FM** fügt die bidirektionale Echtzeit-Audio-Pipeline hinzu (Mic-Capture
   im Browser, Demod-Audio-Wiedergabe) — das aufwändigste neue Stück
   Infrastruktur im ganzen Projekt.
3. **SSB** ist danach nahezu geschenkt — gleiche Audio-Pipeline, nur
   Sideband-Auswahl on top.
4. **M17** beweist, dass sich die Audio-Pipeline auf Digital-Voice
   verallgemeinert (anderer Flowgraph-Innenteil, aber gleicher Transport)
   und bringt zusätzlich den Metadaten-Overlay-Fall (`m17_fields`) mit —
   relevant für spätere Modi wie Meshtastic/MeshCore, die ebenfalls
   strukturierte Empfangsdaten statt nur Audio liefern.

Alle vier sind zudem bereits vollständig serverseitig implementiert
(POCSAG/FM/SSB sogar ganz ohne Zusatz-Install), decken die drei
UI-Grundmuster ab, die alle weiteren Modi wiederverwenden werden
(Wasserfall+Tuning, Audio-Panel, Text/Tabellen-Digimode), und sind
regulatorisch harmlos genug, um sich in V1 nicht mit LoRa-Bandplan-
Sonderfällen aufzuhalten.

**Priorisierung danach** (jede Phase baut nur noch auf bereits bewährten
Mustern auf, kein neues Infrastruktur-Risiko mehr):

- **Phase 2** (kleiner Zusatzaufwand): PSK31, RTTY (identisches
  Text-Digimode-Muster wie POCSAG), Baseband/fldigi-Bridge (Audio-Pipeline
  bereits vorhanden).
- **Phase 3** (mittlerer Aufwand, optionale Installs/neue Widgets): FreeDV
  2020/2020B, RADE, Waterfall Writer (eigenes Wasserfall-Text-Rendering-
  Widget), File Broadcast (Upload/Download-UI, Fortschrittsanzeige aus
  vorhandenen `filebroadcast_*`-Events).
- **Phase 4** (höchster Aufwand): Meshtastic, MeshCore — LoRa-Zusatz-
  Install, Node-Identität/Schlüsselverwaltung in der UI, bandplan-
  abhängige Warnhinweise, Preset-Verwaltung.

## 5. Technologie-Empfehlung

- **Backend:** Python 3, FastAPI + Uvicorn (native WebSocket-Unterstützung,
  einfache REST-Endpunkte für Scan/Health/Login) — passt zur bestehenden
  Python-Codebasis, kein Sprachbruch beim Import von `pluto_tx`/
  `pluto_advanced_rx`.
- **Frontend:** TypeScript + Svelte(Kit) — schlank, gut geeignet für ein
  reaktives Echtzeit-Dashboard mit vielen kleinen State-Updates (Spektrum,
  Audio-Pegel, Digimode-Log); React wäre ebenso machbar, aber ohne
  zusätzlichen Nutzen für diesen Anwendungsfall.
- **Wasserfall/Spektrum:** eigenes WebGL-Canvas-Widget (Texture-Scroll für
  den Wasserfall, Canvas2D-Linie fürs Spektrum) nach dem Vorbild SDR++/
  OpenWebRX — keine serverseitige Bildkodierung, nur rohe dB-Zeilen über
  den Binärkanal.
- **Audio:** Web Audio API (`AudioWorklet`) für Aufnahme (Mic → TX) und
  Wiedergabe (RX → Lautsprecher); PCM16 zum Start, Opus als Ausbaustufe.
- **Persistenz:** SQLite (TX-Log, Konfiguration) — kein separater DB-Server
  nötig für Single-User-Betrieb.
- **Deployment:** vorerst **manueller Start/Stopp** per `scripts/start.sh`/
  `scripts/stop.sh` (ein uvicorn-Prozess liefert auch das gebaute Frontend
  aus) — ein systemd-Dienst kommt erst, wenn der Betrieb eingespielt ist.
  Reverse-Proxy für TLS/WSS, WireGuard-VPN empfohlen für Fernzugriff.

## 6. Repo-Struktur (Vorschlag)

```
Web-TRX/
├── backend/
│   └── web_trx/
│       ├── server.py          # FastAPI-App, WS-Endpunkt
│       ├── session.py         # globale TX/RX-Session-State-Machine
│       ├── spectrum.py        # FftProbe → WS-Binärstream
│       ├── audio.py           # RX-Audio → WS, WS → TX-Audioquelle
│       ├── control.py         # JSON-Steuerprotokoll (Connect/Scan/Mode/PTT/Tune)
│       ├── modes/             # Parameter-Schemas je Modus (spiegelt pluto-cli-Flags)
│       ├── auth.py
│       └── txlog.py
│   └── tests/
├── frontend/
│   └── src/                   # Svelte-App: Waterfall-Widget, Panels, Audio-Worklets
├── scripts/
│   └── start.sh, stop.sh, status.sh  # manueller Betrieb, auch aus den Apps (systemd/Reverse-Proxy später)
└── docs/
    └── PROJECT_PLAN.md         # dieses Dokument
```

## 7. Meilensteine

| # | Meilenstein | Inhalt |
|---|---|---|
| M0 | Grundgerüst | Repo-Struktur, `pluto-tx` als Submodule, Backend-Skeleton (Health-Endpoint), Frontend-Skeleton (Build-Pipeline), CI (Lint/Tests) |
| M1 | Walking Skeleton — POCSAG | Login/Auth, Geräte-Scan/-Connect-UI, POCSAG RX (Log-Tabelle) + TX (RIC/Text-Formular), Session-State-Machine, NOTAUS-Grundgerüst, TX-Log |
| M2 | Wasserfall/Spektrum | `FftProbe`-Bridge, WS-Binärkanal, WebGL-Wasserfall-Widget mit Klick-zum-Tunen, Zoom/Pan, Floor/Ceiling, Fenster-Presets |
| M3 | FM/SSB-Audio | bidirektionale Audio-Pipeline (Mic-Capture → TX, Demod → Wiedergabe), PTT inkl. Latenzmessung, CTCSS/DCS, Power-Ceiling-UI |
| M4 | M17 | Digital-Voice über dieselbe Audio-Pipeline, Rufzeichenfelder, LSF-Metadaten-Anzeige, EOT-Handling |
| M5 | Härtung & Betrieb | Mehrgeräte-Auswahl, Reconnect-Handling, Fehlerdarstellung, Test über echten VPN-Link (Jitter, ggf. Opus-Umstieg), Deployment-Doku |
| M6+ | Phase 2–4 Modi | PSK31, RTTY, Baseband/fldigi → FreeDV, RADE, Waterfall Writer, File Broadcast → Meshtastic, MeshCore, jeweils inkrementell nach Abschnitt 4 |

## 8. Offene Entscheidungen / Risiken

- **Audio-Latenz über VPN/Internet**: PTT-Rückkopplung (Mic → Browser →
  WS → Server → SDR) hat unvermeidbar mehr Latenz als lokales ALSA. Für
  Sprachfunk mit Push-to-Talk unkritisch, sollte aber früh (M3) real
  gemessen werden statt spät anzunehmen.
- **`FftProbe`-Polling-Rate vs. Netzwerkbandbreite**: Zeilenrate/-größe
  müssen an die tatsächliche Client-Bandbreite (LAN vs. VPN/Internet)
  anpassbar sein, nicht fest verdrahtet.
- ~~**Submodule-Pinning-Workflow**~~: erledigt durch die Zusammenführung
  in das pluto-tx-Repository (kein Submodule mehr).

## 9. Aktueller Stand (gegen SimBackend, ohne Hardware verifiziert)

Umgesetzt und per Tests + echtem Chromium-Browser verifiziert (siehe
`docs/DEBUGGING.md`):

- Projekt-Grundgerüst (M0): Backend-/Frontend-Skeleton, `pluto-tx` als
  gepinntes Submodule, CI.
- Walking-Skeleton POCSAG-Roundtrip (M1-Vorgriff): Connect/Select-Mode/
  Tune/PTT/E-STOP komplett über WebSocket, inkl. Auto-Unkey.
- GUI-Politur (SDR++-Stil): dunkles Theme mit hellem Text in allen Feldern/
  Dropdowns, Slider für Floor/Ceiling/Zoom, PTT-Button mit visuellem
  Keyed-Zustand.
- Wasserfall-Interaktion (M2-Vorgriff): Klick-zum-Tunen, Zoom-Slider
  (aktuell client-seitiger Anzeige-Crop, siehe Kommentar in
  `Waterfall.svelte` -- echtes Zoom-FFT ist RX-Hardware-Arbeit).
- Modus-Formulare für alle vier MVP-Modi (FM inkl. CTCSS-Feld, SSB/LSB,
  M17 inkl. Rufzeichenfelder, POCSAG inkl. RIC/Text), Geräte-Scan-Anbindung.
- Audio-Pipeline (M3-Vorgriff, das ohne Hardware machbare Stück):
  `SimBackend` erzeugt einen Dauerton pro RX-Modus, Web Audio-Wiedergabe im
  Browser (`frontend/src/lib/audio.ts`); TX-Mikrofonaufnahme (Press-and-
  Hold-PTT bei Audio-Modi) wird erfasst und über den Binärkanal gesendet,
  vom Backend entgegengenommen. Bewusst mit `ScriptProcessorNode` (nicht
  `AudioWorklet`) gebaut, um ohne zusätzliche Worklet-Build-Schritte sofort
  verifizierbar zu sein -- Umstieg auf `AudioWorklet` ist Härtungsarbeit
  für M5.
- Auth (M1-Vorgriff): geteiltes Passwort (`WEB_TRX_PASSWORD`, sonst beim
  Start generiert und auf stderr ausgegeben), Session-Cookie, Login-
  Screen im Frontend, WS-Handshake wird ohne gültiges Cookie mit HTTP 403
  abgelehnt (nicht erst nach `accept()` getrennt).
- Persistentes TX-Log (M1-Vorgriff): jede Aussendung (Start/Ende, Modus,
  Frequenz, Parameter) landet in SQLite (`web_trx/txlog.py`), angebunden
  über dieselben `keyed`/`unkeyed`/`estop`-Events, die ohnehin durchs
  WS-Protokoll laufen -- funktioniert daher unverändert mit jedem
  `SessionBackend`, nicht nur `SimBackend`. Eigenes TX-Verlauf-Panel im
  Frontend.

`GnuRadioBackend` (`backend/web_trx/radio_backend.py`, auf dem
Radio-Server gegen Pluto+ verifiziert):

- RX: FM/SSB/LSB/M17-Demodulation und POCSAG-Dekodierung (FM + paralleler
  Digimode-Zweig), Wasserfall direkt aus der `FftProbe` des Flowgraphs
  (~25 Zeilen/s), Demod-Audio per eigenem Abgriff-Block in den Browser
  (48 kHz → 16 kHz). Moduswechsel = Flowgraph-Rebuild (~2 s), wie in
  `pluto-cli`.
- TX (M3): FM, SSB, LSB, M17 aus dem Browser-Mikrofon und
  POCSAG. Der lokale ALSA-Mikrofoneingang des TX-Flowgraphs wird vor dem
  Start durch eine eigene, per Wanduhr getaktete Quelle ersetzt, die aus
  einem Jitter-Puffer liest (`backend/web_trx/tx_audio.py`: 80 ms
  Vorpuffer, max. 400 ms, danach wird das Älteste verworfen). Moduswechsel
  live über `set_mode()`, ohne Rebuild. Sicherheit: Watchdog tastet ab,
  wenn 1,5 s kein Mikrofon-Audio kommt (Tab zu, Netz weg), plus
  Sende-Zeitbegrenzung (`WEB_TRX_TX_TIMEOUT_S`, 180 s). Leistungs-Slider
  (bis zur Geräte-Obergrenze, Pluto −20 dB), PTT per Maus/Touch/Leertaste,
  Anzeige Auftast-Latenz und Pufferfüllstand.
- Auf Hardware verifiziert (2026-09-26, Pluto+, 145,150 MHz, −30 dB
  Dämpfung, Mitlesen mit dem RX desselben Pluto): FM mit 1-kHz-Ton
  (PTT → aufgetastet 22–32 ms, Ton im RX-Audio ~350 ms nach PTT), POCSAG
  (Text fehlerfrei dekodiert), M17 (LSF `DA2JH → @ALL` dekodiert),
  Browser-Pfad per Headless-Chromium mit Fake-Mikrofon (keine Aussetzer,
  nichts verworfen). Pro Durchgang loggt das Backend Mikrofon-Statistiken
  (`tx over done: ...`) und zeigt sie im Frontend.
- Wasserfall: echtes Zoom-FFT im Backend (`FftProbe.set_zoom`, ×1…×64,
  bleibt scharf statt pixelig), Video-Mittelung (1…50 Zeilen), FFT-Größe
  wählbar (Default 2048); `hello.features` sagt dem Frontend, ob der
  Server zoomt (sonst — SimBackend — Crop im Browser). Die Seite füllt das
  Browserfenster (Wasserfall nimmt die freie Höhe, Logs feste Höhe).
- Wasserfall: Auto-Pegel am Rauschboden (20-%-Perzentil, geglättet;
  Floor = Rauschen − 5 dB, Bereich 45 dB), eingefroren während eigener
  Aussendung (der RX desselben Geräts übersteuert dann); Peak-Binning pro
  Pixelspalte.
- Frontend übernimmt den Server-Zustand (Frequenz, Modus, Verbindung,
  Leistung, Audio-Einstellungen) aus `hello`/`tuned`/`mode`/`tx_settings`
  — die angezeigte Frequenz ist immer die, auf der der Server sendet.
  Frequenzfeld im SDR++-Stil (Tausenderpunkte, Mausrad je Ziffer, Eingabe
  als Hz oder MHz), RX/TX-Marker im Spektrum. Verbinden startet den
  Flowgraph direkt im gewählten Modus. TX-Audio-Aufbereitung (NF-Pegel,
  Gate, Kompressor, Limiter, Subton-Pegel) und
  Mikrofon-Pegelanzeige im Frontend; Bedien-Einstellungen überleben einen
  Neustart (`run/settings.json`).
- Geräteauswahl im Frontend kommt aus dem `hello`-Event
  (`SessionBackend.device_types()`).

- Modus-Parameter zentral geprüft (`web_trx/modes.py`, spiegelt die
  pluto-cli-Flags): FM-TX mit Hub 2,5/5 kHz, Pre-Emphasis und CTCSS aus der
  Standardtonliste, FM-RX mit De-Emphasis (pluto-tx `c45531d`). Ungültige
  Werte werden zum `error`-Event, Defaults werden ergänzt; die Wertetabellen
  prüft ein Test gegen die pluto-tx-Konfiguration im selben Repository. Das
  Frontend baut seine Auswahllisten aus dem `hello`-Event. Im
  `GnuRadioBackend` landen sie auf `set_fm_deviation()`/
  `set_fm_preemphasis()`/`set_subtone()` bzw. `fm_deemphasis` des
  RX-Flowgraphs (in der Web-Session parallel entstanden, beim Merge
  zusammengeführt — Hub/Pre-Emphasis sind dadurch Modus-Parameter und
  stehen mit im TX-Log, nicht in der Audio-Aufbereitung).

- M5-Härtung:
  - Mikrofon und RX-Wiedergabe laufen als AudioWorklets
    (`frontend/src/lib/worklets/`). Die RX-Wiedergabe hat einen
    Jitter-Puffer: Ziel 120 ms, max. 400 ms, Resampling 16 kHz → Kontextrate.
  - WebSocket-Reconnect mit Backoff; das neue `hello` übernimmt den Zustand.
    Ist die Sitzung ungültig, kommt wieder das Login.
  - Anmeldungen überleben Server-Neustarts (gehashte Tokens in
    `run/sessions.json`), das Passwort bleibt stabil (`web-trx.env`).
  - Der Server öffnet vorher verbundene RX/TX-Richtungen beim Start wieder,
    ohne aufzutasten; nach NOTAUS nicht.
  - Fehler erscheinen als Banner und zusätzlich im betroffenen Panel.
  - Gemessen: RX-Puffer ~180 ms ohne Aussetzer, Mikro 1,94 s in 1,92 s
    (max. Lücke 34 ms statt 85 ms mit ScriptProcessor).

- Neue Modi (auf Hardware verifiziert, 145,150 MHz, −30 dB, Pluto → RTL-SDR):
  - **RADE:** TX aus dem Browser-Mikrofon, RX mit Sync-/SNR-/Ablage-Anzeige.
    Synchronisiert mit SNR 15 dB, Ablage −58 Hz.
  - **RTTY:**
    - TX als Text-Einmalsendung, RX als paralleler Digimode neben USB mit
      AFC-Schritt jede Sekunde; Zeichen werden gesammelt verschickt
      (`rtty_text`, alle 200 ms).
    - Dekodiert fehlerfrei, sobald die Geräteablage korrigiert ist (siehe
      `docs/DEBUGGING.md`). Dafür gibt es jetzt „Korr. ppm“ für RX und TX.
  - **Waterfall Writer (digitext):** Text-Einmalsendung; im Wasserfall
    lesbar mit RX-Bandbreite 0,96 MS/s, FFT 1024, Zoom ×32.
  - Modus-Schemas in `modes.py` (gegen die pluto-tx-Dateien getestet). Die
    verfügbaren Modi meldet das Backend (`features.rx_modes/tx_modes`,
    RADE nur mit librade). RX-Bandbreite (Sample-Rate je Gerät) ist wählbar,
    Zoom bis ×128.

- RX-Verstärkung: Das Backend gibt die `GainStage`-Beschreibung der
  pluto-tx-Geräte samt AGC-Modi als `features.rx_gain` weiter. Das Frontend
  baut daraus die Regler, gespeichert als `gain_mode` und `stage:<NAME>`
  (je Gerät geprüft). Lautstärke ist eine Gain-Node im Browser-Player.
- TX-Leistung: Der Regler reicht immer bis zum Maximum des Geräts (Pluto
  0 dB Dämpfung), anders als pluto-tx' GUI mit „Unlock full power“ (auf
  Wunsch des Betreibers: keine Freigabe, keine Warnung). Sicherheitsnetz
  nur beim Neustart: Eine gespeicherte Leistung über der Geräte-Voreinstellung
  (Pluto −20 dB) wird darauf gekappt.
- Squelch: pluto-tx' RX-Flowgraph hat keinen. Web-TRX hängt
  `analog.probe_avg_mag_sqrd_c` hinter den Kanalfilter (FM:
  `fm_channel_filter`, SSB/LSB/RTTY: `ssb_filter`) und prüft den Pegel
  alle 50 ms (3 dB Hysterese, 0,4 s Haltezeit). Bei geschlossenem Squelch
  geht kein Audio an den Browser; Dekoder bleiben unberührt. Der Pegel geht
  als `rx_level` (5×/s) an die Pegelanzeige. Gemessen: Rauschen ~−43 dB am
  RTL-SDR; Squelch −33 dB → kein Audio, aus → Audio.

- **FT8, Phase F1 (Empfang) nach `docs/FT8_PLAN.md`:**
  - Stationsdaten als Betreiber-Einstellung (`station.py`, `set_station`).
  - FT8-RX im `GnuRadioBackend` (Decoder-Zweig neben USB, `ft8_slot`
    je Slot, `ft8_status`).
  - Direct Sampling für RTL-SDR und NTP-Prüfung (`clock.py`).
  - SimBackend mit erfundenen Decodes, Oberfläche mit Tabelle, Slot-Uhr
    und Bandmarkierung.
  - Gemessen am 28.09.2026, 20 m (14,074 MHz, RTL-SDR Direct Sampling Q),
    nacheinander:
    - pluto-cli: 12 Slots, 74 Decodes.
    - Web-TRX (jt9): 11 Slots, 58 Decodes, davon 3 Anlaufslots mit 0 nach
      mehreren Umbauten direkt nach dem Serverstart.
    - Ohne Umbau je Slot 6/14/3/14/4/15, also gleichauf.
    - Nach einem einzelnen Moduswechsel fehlt nur der angebrochene Slot.

- Unterer Bereich: Empfang (Dekoder), TX-Verlauf und Events als Tabs in
  einem Feld, Standard „Empfang“. Die Höhe zwischen Wasserfall und Tabs
  lässt sich über einen Trenner ziehen (pro Browser gespeichert,
  Doppelklick setzt zurück).

- „Kein Bediener“-Watchdog im `SessionManager`: Verlässt der letzte Browser
  die Sitzung, endet jede Aussendung sofort (`ptt(False)`, bei Fehler
  NOTAUS). Nach `WEB_TRX_NO_OPERATOR_S` (10 s) ohne Browser werden TX und
  RX getrennt; nach dem Serverstart nach 60 s. uvicorn pingt den WebSocket
  alle 10 s, damit auch stumm abgerissene Verbindungen erkannt werden.
  Gilt für jedes Backend und jeden Modus; der Mikrofon-Watchdog (1,5 s)
  bleibt zusätzlich.

Noch offen: Latenzmessung über echten VPN-Link, Opus über langsame Links;
FT8 Phasen F2–F4 (Senden).
