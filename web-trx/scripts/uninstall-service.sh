#!/usr/bin/env bash
# Removes the systemd service set up by scripts/install-service.sh: stops the
# server (stop.sh -> TX safe state), disables the autostart and deletes the
# unit. Web-TRX itself stays installed -- start it by hand again with
# "web-trx start". web-trx.env (password) and run/ (certificate, TX log,
# settings) are left alone.
#
# Run as the normal user; calls sudo itself. Safe to re-run.
#
# Exit codes: 0 removed (or was not installed), 1 error.
set -euo pipefail

if [[ "$(id -u)" == "0" ]]; then
  echo "FEHLER: Nicht mit sudo oder als root ausführen." >&2
  echo "Richtig: web-trx/scripts/uninstall-service.sh   (das Skript ruft sudo selbst auf)" >&2
  exit 1
fi

SERVICE=web-trx.service
UNIT_FILE="/etc/systemd/system/$SERVICE"
DROPIN_DIR="/etc/systemd/system/$SERVICE.d"
OLD_DROPIN="$DROPIN_DIR/headless-audio.conf"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd nicht gefunden -- nichts zu entfernen."
  exit 0
fi
if [[ ! -f "$UNIT_FILE" && ! -f "$OLD_DROPIN" ]]; then
  echo "Web-TRX-Dienst ist nicht eingerichtet -- nichts zu entfernen."
  exit 0
fi

echo "== Web-TRX-Dienst entfernen =="
echo "(sudo-Passwort wird ggf. abgefragt)"
# disable --now runs ExecStop (stop.sh), i.e. unkey + TX safe state.
sudo systemctl disable --now "$SERVICE" 2>/dev/null || true
if systemctl is-active --quiet "$SERVICE"; then
  echo "FEHLER: Dienst lässt sich nicht stoppen -- systemctl status $SERVICE prüfen." >&2
  exit 1
fi

sudo rm -f "$UNIT_FILE" "$OLD_DROPIN"
if [[ -d "$DROPIN_DIR" ]]; then
  sudo rmdir --ignore-fail-on-non-empty "$DROPIN_DIR"
  if [[ -d "$DROPIN_DIR" ]]; then
    echo "HINWEIS: $DROPIN_DIR enthält noch eigene Dateien und bleibt stehen."
  fi
fi
sudo systemctl daemon-reload
sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true

echo
echo "== Fertig =="
echo "Web-TRX startet nicht mehr automatisch und ist gestoppt."
echo "Von Hand starten: web-trx start"
