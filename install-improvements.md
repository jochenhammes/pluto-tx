# Verbesserungsplan

Vier konkrete Probleme wurden bei einer realen Installation und einem
ersten HackRF-Sendetest gefunden. Die ersten drei betreffen die
Install-Skripte, das vierte den App-Code selbst.

---

## Problem 1: Mit `sudo` ausgeführt → `$HOME=/root` → falsche Pfade

### Betroffene Skripte
`install.sh`, `install-m17.sh`, `install-rade.sh`

### Was passiert
Alle drei Skripte verwenden `$HOME` um Launcher-Skripte und (bei m17/rade)
den `CMAKE_INSTALL_PREFIX` zu bestimmen. Wenn das Skript mit
`sudo ./install-m17.sh` statt `./install-m17.sh` aufgerufen wird,
setzt sudo `HOME=/root`. Damit landen alle Artefakte in `/root/.local/`
statt in `~user/.local/` — der Benutzer findet hinterher weder
`pluto-tx` im PATH noch funktioniert der Import, weil die `.so`-Dateien
an der falschen Stelle liegen.

Konkret beobachtet: `install-m17.sh` wurde mit sudo aufgerufen.
`make install` installierte nach `/root/.local/lib/`, der Launcher
wurde nach `/root/.local/bin/pluto-tx` geschrieben.
`from gnuradio import m17` schlug danach fehl.

### Fix für `install-m17.sh` und `install-rade.sh`

Diese beiden Skripte brauchen **kein** sudo für den eigentlichen Build,
nur für die optionalen apt-Pakete am Anfang. Dort rufen sie selbst
`sudo apt-get` auf — das Skript als Ganzes soll ohne sudo laufen.

Direkt nach dem `set -euo pipefail` einfügen:

```bash
if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausführen." >&2
    echo "Richtig: ./install-m17.sh   (das Skript ruft sudo intern für apt-get auf)" >&2
    exit 1
fi
```

### Fix für `install.sh`

`install.sh` ruft intern `sudo apt-get` auf, läuft aber selbst als
normaler Benutzer — das ist korrekt. Trotzdem kann ein Benutzer
`sudo ./install.sh` aufrufen. Gleicher Guard wie oben einfügen.

Zusätzlich: Falls `$HOME` trotzdem auf `/root` zeigt (kann in manchen
Container-/CI-Umgebungen vorkommen), als letztes Mittel `SUDO_USER`
auswerten:

```bash
# Direkt nach dem id-u-Check:
if [ -n "${SUDO_USER:-}" ]; then
    # Skript wurde doch mit sudo aufgerufen; HOME korrigieren.
    HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    export HOME
    echo "HINWEIS: HOME auf $HOME korrigiert (SUDO_USER=$SUDO_USER)." >&2
fi
```

---

## Problem 2: `install-rade.sh` hängt ohne Timeout bei `media.xiph.org`

### Was passiert
`cmake` lädt als ExternalProject die gepatche Opus-Fork herunter und baut
sie. Dabei ruft `autogen.sh` der Fork `dnn/download_model.sh` auf, das
per `wget` ein FARGAN/LPCNet-Modell (~100 MB) von `media.xiph.org`
herunterlädt. Das wget hat **keinen Timeout-Flag**. Wenn `media.xiph.org`
nicht erreichbar ist (Server down, Routing-Problem), hängt der Prozess
einfach — ohne Fehlermeldung, ohne Abbruch.

`download_model.sh` liegt im Quellbaum der Opus-Fork und kann nicht
direkt in `install-rade.sh` gepatcht werden (er existiert erst nach dem
Download und Entpacken durch cmake).

### Fix: wget-Wrapper mit Timeout vor cmake in PATH setzen

Vor dem `cmake`-Aufruf einen temporären Wrapper erstellen, der
`wget --timeout=30 --tries=2` erzwingt. Da PATH vor allen Systemtools
durchsucht wird, greift der Wrapper transparent — auch für Code tief
im ExternalProject-Build.

```bash
# Vor dem cmake-Aufruf in install-rade.sh einfügen:
WGET_WRAP_DIR="$(mktemp -d)"
cat > "$WGET_WRAP_DIR/wget" <<'WGET_EOF'
#!/bin/sh
exec /usr/bin/wget --timeout=30 --tries=2 "$@"
WGET_EOF
chmod +x "$WGET_WRAP_DIR/wget"
export PATH="$WGET_WRAP_DIR:$PATH"

# ... cmake -DCMAKE_BUILD_TYPE=Release .. ...
# ... make -j"$(nproc)" ...

# Danach aufräumen:
rm -rf "$WGET_WRAP_DIR"
```

Mit `--timeout=30` bricht wget nach 30 Sekunden Inaktivität ab,
`--tries=2` versucht es einmal erneut. Der Build schlägt dann mit einer
klaren Fehlermeldung von cmake fehl statt ewig zu hängen.

**Wo genau einfügen:** Unmittelbar vor `mkdir -p "$RADE_C_DIR/build"`.
`rm -rf "$WGET_WRAP_DIR"` am Ende des Skripts, nach `cd "$SCRIPT_DIR"`.
Für sicheres Aufräumen auch bei Fehlern: `trap 'rm -rf "$WGET_WRAP_DIR"' EXIT`
statt des manuellen `rm`.

Vollständige Einfügestelle (ersetzt den bisherigen cmake-Block):

```bash
echo
echo "Configuring and building rade_c..."
mkdir -p "$RADE_C_DIR/build"

# wget-Wrapper: erzwingt Timeout damit media.xiph.org-Hänger
# nicht den ganzen Build blockieren.
WGET_WRAP_DIR="$(mktemp -d)"
trap 'rm -rf "$WGET_WRAP_DIR"' EXIT
cat > "$WGET_WRAP_DIR/wget" <<'WGET_EOF'
#!/bin/sh
exec /usr/bin/wget --timeout=30 --tries=2 "$@"
WGET_EOF
chmod +x "$WGET_WRAP_DIR/wget"
export PATH="$WGET_WRAP_DIR:$PATH"

cd "$RADE_C_DIR/build"
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j"$(nproc)"
cd "$SCRIPT_DIR"
```

---

## Problem 3: `install-m17.sh` überschreibt den Launcher destruktiv

### Was passiert
`install-m17.sh` schreibt den `pluto-tx`-Launcher mit einem
`cat > ... <<EOF`-Block komplett neu — ohne zu prüfen, ob bereits ein
`LD_LIBRARY_PATH`-Eintrag von `install-rade.sh` vorhanden ist.

Wenn also RADE zuerst installiert wurde und danach `install-m17.sh`
läuft, verliert der Launcher den RADE-Pfad. M17 funktioniert, RADE ist
ausgegraut.

`install-rade.sh` hat dieses Problem bereits gelöst (merge-sichere
`prepend_if_missing`-Funktion). `install-m17.sh` sollte dasselbe Muster
verwenden.

### Fix

Die `prepend_if_missing`- und `regenerate_launcher`-Funktionen aus
`install-rade.sh` in `install-m17.sh` übernehmen. Den bisherigen
`cat > ~/.local/bin/pluto-tx`-Block ersetzen durch:

```bash
prepend_if_missing() {
    local dir="$1" list="$2"
    if [ -z "$list" ]; then echo "$dir"; return; fi
    local IFS=':' part
    for part in $list; do
        [ "$part" = "$dir" ] && { echo "$list"; return; }
    done
    echo "$dir:$list"
}

regenerate_launcher() {
    local name="$1" module_invocation="$2"
    local launcher="$HOME/.local/bin/$name"
    local existing_ld=""
    if [ -f "$launcher" ]; then
        existing_ld="$(grep -oP '(?<=export LD_LIBRARY_PATH=")[^"]*(?=:\$\{LD_LIBRARY_PATH:-\}")' \
            "$launcher" 2>/dev/null || true)"
    fi
    local new_ld
    new_ld="$(prepend_if_missing "$LIBDIR" "$existing_ld")"
    mkdir -p "$HOME/.local/bin"
    cat > "$launcher" <<EOF
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$new_ld:\${LD_LIBRARY_PATH:-}"
cd "$SCRIPT_DIR" && exec python3 -m $module_invocation "\$@"
EOF
    chmod +x "$launcher"
    echo "Updated $launcher"
}

echo
echo "Updating the pluto-tx launcher..."
regenerate_launcher "pluto-tx" "pluto_tx.app --gui"
```

Den bisherigen Block (Zeilen 126–133 in `install-m17.sh`) durch diesen
Aufruf ersetzen.

---

---

## Problem 4: HackRF-Default-Samplerate (8 Msps) überlastet CPU bei FM

### Betroffene Datei
`pluto_tx/devices/hackrf.py`, Zeile 41: `DEFAULT_SAMPLE_RATE_HZ = 8_000_000`

### Was passiert
Beim ersten FM-Sendetest mit HackRF auf einem Intel i5-6300U (Dual-Core
Laptop, 2016) produzierte die App ein abgehacktes, unverständliches Signal
am Empfänger. Ursache: GNU-Radio-Buffer-Underruns — die CPU schafft es
nicht, den HackRF-USB-Puffer kontinuierlich zu befüllen.

### Warum 8 Msps zu viel ist

Im Flowgraph laufen bei 8 Msps gleichzeitig zwei teure Blöcke:

1. **`fm_resampler_fff` (48 kHz → 8 Msps):** GCD = 16.000, daraus
   Interpolation = **500**, Dezimation = 3. Ein polyphasen FIR mit
   500 Armen — der mit Abstand teuerste Block im Flowgraph.

2. **Wasserfall-Resampler (8 Msps → 50 kHz):** Ratio 160:1, braucht
   einen sehr engen Antialiasing-Filter, läuft am vollen 8-Msps-Eingang.

Der Code-Kommentar in `hackrf.py` behauptet, 8 Msps habe
„comparable computational cost" zu Plutos 2,5 Msps. Das stimmt nicht:
Pluto verarbeitet 3,2× weniger Samples pro Sekunde, und der
Wasserfall-Resampler läuft auf 2,5 statt 8 Msps. Tatsächliche Last
beim Resampler: proportional zu (Samplerate × Filterarme). Bei 8 Msps
mit 500 Armen gegenüber 2,5 Msps mit 625 Armen ist der HackRF-Pfad
~2,6× teurer — plus dem vollen Wasserfall-Overhead.

### Warum 2 Msps ausreicht

Für Schmalband-FM mit ±2,5 kHz Hub (`FM_DEVIATION_HZ = 2500` in
`config.py`) belegt das Signal weniger als 10 kHz Bandbreite. 2 Msps
entspricht 1 MHz nutzbarer Bandbreite — 100× mehr als das FM-Signal
breit ist. Die Signalqualität ist identisch.

Bei 2 Msps:
- GCD(2.000.000, 48.000) = 16.000 → Interpolation = **125**, Dezimation = 3
- Resampler-Last: 125 Arme × 4× weniger Samples/s ≈ **~16× geringere
  Gesamtlast** gegenüber 8 Msps/500 Arme
- Wasserfall-Resampler läuft auf 2 Msps statt 8 Msps: weiterer
  Faktor 4×

### Fix

In `pluto_tx/devices/hackrf.py` den Default-Wert und den Kommentar
anpassen:

```python
# 2 Msps: ausreichend für alle Sprachmodi (FM ±2,5 kHz, SSB, M17,
# FreeDV, RADE -- kein Modus braucht mehr als ~200 kHz Bandbreite).
# 8 Msps erwies sich auf einem i5-6300U (Dual-Core Laptop) als zu hoch:
# der fm_resampler_fff 48k→8M (Interpolation 500, 500 Polyphasen-Arme)
# plus der Wasserfall-Resampler 8M→50k überlasteten die CPU und
# verursachten Buffer-Underruns (abgehacktes Signal am Empfänger).
# Bei 2 Msps: Interpolation 125, ~16× geringere Resampler-Last.
DEFAULT_SAMPLE_RATE_HZ = 2_000_000
```

Die `SAMPLE_RATE_RANGE_HZ`-Grenzen (2–20 Msps) bleiben unverändert —
wer ein leistungsstarkes Desktop-System hat und aus anderen Gründen
eine höhere Rate will (z. B. breitbandigerer Wasserfall), kann den Wert
manuell setzen. Sinnvoll wäre außerdem, die Sample-Rate langfristig
als editierbares Feld in der GUI verfügbar zu machen (ähnlich dem
Frequenz-Feld), damit kein Code-Edit nötig ist — das ist aber ein
separates GUI-Feature.

---

## Zusammenfassung der Änderungen

| Datei | Änderung |
|---|---|
| `install.sh` | `id -u`-Guard + optionale `SUDO_USER`-HOME-Korrektur oben |
| `install-m17.sh` | `id -u`-Guard oben; Launcher-Regeneration merge-sicher wie in `install-rade.sh` |
| `install-rade.sh` | `id -u`-Guard oben; wget-Wrapper mit `--timeout=30` vor cmake |
| `pluto_tx/devices/hackrf.py` | `DEFAULT_SAMPLE_RATE_HZ` von 8.000.000 auf 2.000.000 setzen; Kommentar aktualisieren |
