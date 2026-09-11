# pluto-tx: Datei-Broadcast-Erweiterung — Briefing

**Kontext:** Erweiterung von [pluto-tx](https://github.com/jochenhammes/pluto-tx) um repetitiven File-Broadcast auf dem 23cm-Band. Ziel: Empfänger können sich jederzeit einklinken, bekommen periodisch eine Filelist mit Metainformationen und entscheiden selbst, welche Dateien sie aufzeichnen. Dateien werden in Chunks zerlegt und wiederholt gesendet.

## Gewählter Ansatz: PACSAT Broadcast Protocol

Kein Ad-hoc-Design nötig — es gibt dafür seit 1990 einen etablierten Amateurfunk-Standard, der exakt für dieses Szenario entwickelt wurde: Store-and-Forward-Satelliten (PACSAT/UoSAT), die Dateien an beliebig viele, nicht angemeldete Bodenstationen broadcasten.

**Kernmechanismen (direkt übertragbar):**

- **PACSAT File Header (PFH)** — standardisierte Metadaten pro Datei: Größe, Prüfsummen, Typ, Quelle. Wird periodisch als Directory-Broadcast gesendet, damit Empfänger vor dem Download entscheiden können, ob sie eine Datei aufzeichnen wollen.
- **Chunked File-Broadcast** — Dateien in nummerierte Blöcke zerlegt, jeder Block trägt Offset + Länge, ist also für sich allein verständlich. Rotationsplan sendet Blöcke wiederholt (round-robin, ggf. priorisiert nach Downloadzähler).
- **Zustandsloser Sender** — Sender kennt seine Zuhörer nicht, sendet einfach den Rotationsplan durch. Kein Verbindungsaufbau nötig.
- **"Holes"-Tracking beim Empfänger** — Ground-Station-Software merkt sich pro Datei, welche Byte-Bereiche fehlen, füllt sie über mehrere Zyklen/Durchgänge auf. Genau das Prinzip "jederzeit einsteigen, Lücken schließen sich über Zeit".

## Warum dieser Ansatz statt Eigenentwicklung

- Offen dokumentierter, seit Jahrzehnten stabiler Standard (AMSAT-Spezifikationen), keine Neuerfindung nötig.
- Aktive moderne Referenzimplementierungen vorhanden:
  - AMSAT entwickelt aktuell offen auf GitHub ein neues PACSAT (Hardware + Flight Software).
  - g0kla pflegt eine moderne Java-Ground-Station-Software (PacSat Ground Station, 2018–2023) für dieses Protokoll.
- **gr-satellites** (aktiv gepflegtes GNU-Radio-Modul, gleiches Ökosystem wie pluto-tx) kann bereits AX.25-Frames deframen und Dateien aus Telemetrie reassemblieren — vorhandene PDU-Bausteine ließen sich als Basis für Deframing/File-Reassembly nutzen statt bei null anzufangen.

## Wichtige Abgrenzung

Das PACSAT-Protokoll legt nur die **Struktur der Nutzdaten** fest (PFH + Blockheader/Rotationslogik) — nicht zwingend Modulation oder Bitrate. Klassisch läuft es über AX.25 auf 1200 Baud, das ist für 23cm-Bandbreite viel zu langsam. Für pluto-tx heißt das: eigenes PHY (z.B. GFSK auf 23cm, wie in der ursprünglichen PHY-Diskussion skizziert) mit dem PACSAT-Framing kombinieren, nicht die AX.25-typischen Bitraten übernehmen.

## Geplante Architektur in pluto-tx

Wie M17/FreeDV: eigener Modus, der `mode_selector` umgeht (Lehre aus M17: hohe Symbolraten/Expansionsfaktoren bringen den GNU-Radio-Scheduler in gemeinsamer Führung zum Stocken).

- **Sendeseite** (`pluto_tx`, neuer Modus): PFH-Generator + Chunker + Rotationsplaner.
- **Empfangsseite** (`pluto_advanced_rx` oder eigene App): Frame-Deframer, Directory-Tabelle mit Holes-Tracking, File-Reassembler. Orientierung an gr-satellites' vorhandenen PDU-Blöcken sinnvoll.

## Offene Punkte für die nächste Session

- PFH-Format und Blockheader-Struktur im Detail durchgehen, um zu entscheiden, wie viel direkt übernommen wird.
- Konkrete PHY-Parameter für 23cm festlegen (Symbolrate, Modulation — siehe frühere Diskussion zu GFSK/4FSK vs. OFDM).
- Rotationsplaner-Logik definieren (round-robin, Prioritäten, Umgang mit neu hinzugefügten Dateien während laufendem Zyklus).

## Verworfene/zurückgestellte Alternativen (zur Erinnerung)

- **HNAP4PlutoSDR** — fertiger OFDM-IP-Link für PlutoSDR (70cm, rekonfigurierbar), würde Dateiübertragung trivial über normale IP-Tools lösen, passt aber nicht zum gewünschten Broadcast-Charakter (baut Punkt-zu-Multipunkt-IP-Link auf, kein "jederzeit einklinken ohne Session").
- **New Packet Radio (NPR)** — TDMA/GFSK-Standard, aber eigene Hardware, kein GNU-Radio/Pluto-Ansatz, eher als Designreferenz interessant.
- **AX.25/KISS + IL2P/FX.25** — der "klassische" Paketfunk-Standard, aber zu geringe Bitraten für 23cm-Bandbreitennutzung.
- **Winlink/ARDOP/VARA/Mercury** — etablierte Dateiübertragungsstandards, aber für schmalbandigen HF-ARQ-Betrieb ausgelegt, kein Broadcast-Charakter.
