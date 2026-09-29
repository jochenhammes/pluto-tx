#!/usr/bin/env bash
# Sets Web-TRX up as a systemd system service for headless operation: the
# server starts at boot, before (and without) any user login, and restarts
# after a crash. Undo with scripts/uninstall-service.sh.
#
# The service runs scripts/start.sh/stop.sh as the calling user (not root) --
# start.sh finds gr-m17/gr-lora_sdr and the pip packages under that user's
# $HOME/.local, and HackRF/RTL-SDR access comes from the plugdev group. It
# shares the PID file and lock with "web-trx start/stop" and the apps' Web-TRX
# row, so "web-trx status/log" keep working.
#
# Sound: the TX flowgraph always opens a local audio sink/source on the system
# default device, even for Pluto/HackRF. Before login there is no PipeWire, so
# ALSA "default" fails ("audio_alsa_sink :error: [default]: Host is down") and
# the whole TX connect fails with it. The unit points GNU Radio's ALSA default
# at ALSA's "null" device instead (Web-TRX takes the microphone from the
# browser anyway). Explicitly named devices (AIOC plughw:...) are unaffected.
#
# Run as the normal user; calls sudo itself where needed. Safe to re-run
# (rewrites the unit and restarts the service).
#
# Exit codes: 0 installed and running, 1 error.
set -euo pipefail

if [[ "$(id -u)" == "0" ]]; then
  echo "FEHLER: Nicht mit sudo oder als root ausführen -- der Dienst soll als dein Benutzer laufen." >&2
  echo "Richtig: web-trx/scripts/install-service.sh   (das Skript ruft sudo selbst auf)" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # web-trx/
SERVICE=web-trx.service
UNIT_FILE="/etc/systemd/system/$SERVICE"
# Left over from an earlier manual setup of the sound fix -- now part of the unit.
OLD_DROPIN="/etc/systemd/system/$SERVICE.d/headless-audio.conf"
SVC_USER="$(id -un)"
SVC_GROUP="$(id -gn)"

if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  echo "FEHLER: systemd nicht gefunden -- dieses Skript braucht einen systemd-Rechner." >&2
  exit 1
fi
if [[ "$ROOT" =~ [[:space:]] ]]; then
  echo "FEHLER: Pfad enthält Leerzeichen ($ROOT) -- für die systemd-Unit nicht unterstützt." >&2
  exit 1
fi
if [[ ! -x "$ROOT/backend/.venv/bin/uvicorn" || ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "FEHLER: Web-TRX ist nicht installiert -- zuerst ./install-web-trx.sh im pluto-tx-Verzeichnis ausführen." >&2
  exit 1
fi

echo "== Web-TRX als Dienst einrichten (headless) =="
echo "Verzeichnis: $ROOT"
echo "Benutzer:    $SVC_USER"
echo

# --- checks that only warn ---------------------------------------------------
# A Wi-Fi profile restricted to one user, or with its password in the user's
# keyring (psk-flags != 0), only comes up after that user logs in -- then the
# server would be running but unreachable on a headless machine.
if command -v nmcli >/dev/null 2>&1; then
  while IFS=: read -r uuid type; do
    [[ "$type" == 802-11-wireless ]] || continue
    info="$(nmcli -g connection.id,connection.permissions,802-11-wireless-security.psk-flags connection show "$uuid" 2>/dev/null || true)"
    name="$(sed -n 1p <<<"$info")"
    perms="$(sed -n 2p <<<"$info")"
    flags="$(sed -n 3p <<<"$info")"
    if [[ -n "$perms" || ( -n "$flags" && "$flags" != 0* ) ]]; then
      echo "WARNUNG: WLAN \"$name\" verbindet sich erst nach der Anmeldung (Profil nur für einen Benutzer"
      echo "         oder Passwort im Schlüsselbund). Für Headless-Betrieb in den Netzwerk-Einstellungen"
      echo "         \"Für alle Benutzer verfügbar\" setzen und das Passwort für alle Benutzer speichern."
    fi
  done < <(nmcli -t -f UUID,TYPE connection show --active 2>/dev/null || true)
fi
if ! id -nG | tr ' ' '\n' | grep -qx plugdev; then
  echo "WARNUNG: $SVC_USER ist nicht in der Gruppe plugdev -- HackRF/RTL-SDR gehen dann im Dienst nicht (./install.sh richtet das ein)."
fi

# --- the unit ----------------------------------------------------------------
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
cat >"$TMP_UNIT" <<EOF
# Written by web-trx/scripts/install-service.sh -- remove with uninstall-service.sh
[Unit]
Description=Web-TRX (pluto-tx browser interface)
Documentation=file://$ROOT/README.md
Wants=network-online.target
After=network-online.target avahi-daemon.service

[Service]
User=$SVC_USER
Group=$SVC_GROUP
WorkingDirectory=$ROOT

# No PipeWire before login: GNU Radio's ALSA default -> ALSA "null" device.
Environment=GR_CONF_AUDIO_ALSA_DEFAULT_OUTPUT_DEVICE=null
Environment=GR_CONF_AUDIO_ALSA_DEFAULT_INPUT_DEVICE=null

# start.sh backgrounds uvicorn, writes the PID file and exits 0 once the
# server has confirmed startup.
Type=forking
PIDFile=$ROOT/run/web-trx.pid
ExecStart=$ROOT/scripts/start.sh
# stop.sh: SIGTERM -> unkey + TX safe state (Pluto: max attenuation, LO off)
ExecStop=$ROOT/scripts/stop.sh
TimeoutStartSec=120
TimeoutStopSec=40

# Restart after a crash, not after a deliberate "web-trx stop".
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

if [[ -f "$UNIT_FILE" ]] && ! grep -qF "ExecStart=$ROOT/scripts/start.sh" "$UNIT_FILE"; then
  echo "HINWEIS: Die vorhandene $SERVICE gehört zu einem anderen Checkout und wird ersetzt."
fi

# A server started by hand ("web-trx start", the apps) holds the PID file --
# the service's start.sh would then only report "already running" and fail.
if ! systemctl is-active --quiet "$SERVICE"; then
  "$ROOT/scripts/stop.sh" || true
fi

echo "Installiere $UNIT_FILE (sudo-Passwort wird ggf. abgefragt) ..."
sudo install -m 644 "$TMP_UNIT" "$UNIT_FILE"
if [[ -f "$OLD_DROPIN" ]]; then
  sudo rm -f "$OLD_DROPIN"
  sudo rmdir --ignore-fail-on-non-empty "$(dirname "$OLD_DROPIN")"
fi
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
echo "Starte Dienst ..."
if ! sudo systemctl restart "$SERVICE"; then
  echo "FEHLER: Dienst startet nicht. Details: systemctl status $SERVICE / web-trx log" >&2
  exit 1
fi

echo
"$ROOT/scripts/status.sh" || true
echo
echo "== Fertig =="
echo "Web-TRX startet jetzt bei jedem Systemstart, auch ohne Anmeldung."
echo "Stoppen/Starten: sudo systemctl stop|start|restart web-trx"
echo "                 (\"web-trx stop\" geht auch, dann läuft Web-TRX erst nach dem nächsten Boot wieder)"
echo "Status/Log:      web-trx status / web-trx log / journalctl -u web-trx"
echo "Entfernen:       web-trx/scripts/uninstall-service.sh"
