#!/usr/bin/env bash
# Stops Web-TRX started by scripts/start.sh. SIGTERM lets uvicorn run the app
# shutdown, which unkeys and forces the TX device into its safe state
# (PlutoTxFlowgraph.shutdown_safe()) before the process exits.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/web-trx.pid"

if [[ ! -f "$PID_FILE" ]] || ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Web-TRX läuft nicht."
  rm -f "$PID_FILE"
  exit 0
fi

PID="$(cat "$PID_FILE")"
kill -TERM "$PID"
for _ in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Web-TRX gestoppt."
    exit 0
  fi
  sleep 0.5
done

echo "Kein sauberes Ende nach 15 s -- erzwinge Abbruch (PID $PID)."
echo "Achtung: Safe-State des Senders prüfen (z. B. pluto-cli oder iio_attr)."
kill -KILL "$PID"
rm -f "$PID_FILE"
