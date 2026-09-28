#!/usr/bin/env bash
# M1 -- Baseline: the test results BEFORE the merge, per test ID, in scratch
# clones under $WORK_DIR/baseline (never in the production checkouts).
#   results/baseline-pluto-tx.json   python3 -m unittest discover tests
#   results/baseline-web-trx.xml     pytest of the old Web-TRX backend (old venv)
#   results/baseline-frontend.txt    npm ci / check / build of the old frontend
# A red test here is not a blocker by itself -- it is "pre-existing" and
# 40_verify.sh only demands that nothing gets worse. Re-runnable.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config
start_log 10_baseline

BASE="$WORK_DIR/baseline"
rm -rf "$BASE"
mkdir -p "$BASE" "$RESULTS_DIR"

log "pluto-tx: Scratch-Klon von $PLUTO_TX_DIR"
git clone -q --no-hardlinks "$PLUTO_TX_DIR" "$BASE/pluto-tx"
link_builds "$PLUTO_TX_DIR" "$BASE/pluto-tx"
git -C "$BASE/pluto-tx" log -1 --oneline
log "pluto-tx: Tests (dauert einige Minuten)"
set +e
(cd "$BASE/pluto-tx" && run_clean python3 "$MERGE_SCRIPTS_DIR/unittest_json.py" "$RESULTS_DIR/baseline-pluto-tx.json") \
  >"$RESULTS_DIR/baseline-pluto-tx.txt" 2>&1
rc_tx=$?
set -e
tail -n 4 "$RESULTS_DIR/baseline-pluto-tx.txt"
log "pluto-tx: Exit $rc_tx, Ergebnisse je Test: $RESULTS_DIR/baseline-pluto-tx.json"

SHA="$(import_sha)"
log "Web-TRX: Scratch-Klon von $WEB_TRX_OLD_DIR @ $SHA"
git clone -q --no-hardlinks "$WEB_TRX_OLD_DIR" "$BASE/Web-TRX"
git -C "$BASE/Web-TRX" checkout -q "$SHA"
# the submodule from the local checkout (no network), so the table tests run
if [[ -d "$WEB_TRX_OLD_DIR/vendor/pluto-tx/.git" || -f "$WEB_TRX_OLD_DIR/vendor/pluto-tx/.git" ]]; then
  git -C "$BASE/Web-TRX" submodule init -q
  git -C "$BASE/Web-TRX" config submodule.vendor/pluto-tx.url "$WEB_TRX_OLD_DIR/vendor/pluto-tx"
  git -C "$BASE/Web-TRX" -c protocol.file.allow=always submodule update -q || warn "Submodule nicht auscheckbar -- Tabellentests werden übersprungen"
fi

OLD_PY="$WEB_TRX_OLD_DIR/backend/.venv/bin/python"
if [[ -x "$OLD_PY" ]]; then
  log "Web-TRX-Backend: pytest mit dem alten venv (importiert web_trx aus dem Klon)"
  set +e
  (cd "$BASE/Web-TRX/backend" && run_clean "$OLD_PY" -m pytest -q -p no:cacheprovider \
      --junitxml="$RESULTS_DIR/baseline-web-trx.xml") >"$RESULTS_DIR/baseline-web-trx.txt" 2>&1
  rc_web=$?
  set -e
  tail -n 3 "$RESULTS_DIR/baseline-web-trx.txt"
  log "Web-TRX-Backend: Exit $rc_web"
  "$WEB_TRX_OLD_DIR/backend/.venv/bin/pip" freeze >"$RESULTS_DIR/old-venv-freeze.txt" 2>/dev/null || true
else
  warn "kein altes venv -- keine Backend-Baseline"
  rc_web=skipped
fi

if command -v npm >/dev/null 2>&1; then
  log "Web-TRX-Frontend: npm ci, check, build"
  set +e
  (cd "$BASE/Web-TRX/frontend" && npm ci --no-audit --no-fund && npm run check && npm run build) \
    >"$RESULTS_DIR/baseline-frontend.txt" 2>&1
  rc_fe=$?
  set -e
  log "Web-TRX-Frontend: Exit $rc_fe"
else
  warn "npm fehlt -- keine Frontend-Baseline"
  rc_fe=skipped
fi

cat >"$RESULTS_DIR/baseline-summary.txt" <<EOF
pluto-tx HEAD:   $(git -C "$BASE/pluto-tx" rev-parse HEAD)
Web-TRX @:       $SHA
pluto-tx tests:  exit $rc_tx
web-trx pytest:  exit $rc_web
frontend:        exit $rc_fe
EOF
echo
cat "$RESULTS_DIR/baseline-summary.txt"
log "Baseline fertig. HALT: Ergebnis dem Betreiber zeigen (rote Tests = vorbestehend, nicht durch den Merge)."
