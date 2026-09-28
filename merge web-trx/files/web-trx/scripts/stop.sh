#!/usr/bin/env bash
# Stops Web-TRX started by scripts/start.sh. SIGTERM lets uvicorn run the app
# shutdown, which unkeys and forces the TX device into its safe state
# (PlutoTxFlowgraph.shutdown_safe()) before the process exits.
#
# Exit codes: 0 stopped (or was not running), 2 had to be killed with
# SIGKILL -- check the transmitter's safe state, 1 error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # web-trx/
RUN_DIR="$ROOT/run"
PID_FILE="$RUN_DIR/web-trx.pid"

mkdir -p "$RUN_DIR"

# Same lock as start.sh: a stop during a running start waits for it.
exec 9>"$RUN_DIR/start.lock"
if ! flock -w 70 9; then
  echo "Ein anderer Start/Stopp von Web-TRX läuft noch (Sperre $RUN_DIR/start.lock)." >&2
  exit 1
fi

is_webtrx_pid() {
  [[ "$1" =~ ^[0-9]+$ ]] && kill -0 "$1" 2>/dev/null \
    && grep -qa uvicorn "/proc/$1/cmdline" 2>/dev/null \
    && grep -qa web_trx.server "/proc/$1/cmdline" 2>/dev/null
}

if [[ ! -f "$PID_FILE" ]]; then
  echo "Web-TRX läuft nicht."
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if ! is_webtrx_pid "$PID"; then
  # Never signal a process we did not start: after a reboot the old PID can
  # belong to anything.
  echo "Web-TRX läuft nicht (veraltete PID-Datei mit PID '${PID}' entfernt)."
  rm -f "$PID_FILE"
  exit 0
fi

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
kill -KILL "$PID" 2>/dev/null || true
for _ in $(seq 1 10); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.2
done
rm -f "$PID_FILE"
exit 2
