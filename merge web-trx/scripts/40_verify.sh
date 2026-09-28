#!/usr/bin/env bash
# M4 -- verifies the merge in the scratch clone, without hardware:
#   quick (also after every commit of 30_build_merge.sh):
#     changed files only from the allowlist; gui.py: no deleted line;
#     no gitlink/.gitmodules; shell syntax; Python compiles; tree clean
#   full (default) additionally:
#     import complete (blob + mode per file vs. the Web-TRX commit);
#     ignore rules; shellcheck; pluto-tx tests vs. baseline per test ID;
#     install-web-trx.sh with a throw-away HOME; web-trx pytest/ruff vs.
#     baseline; frontend check; script smoke test (sim backend, port
#     $SMOKE_PORT, plain http) incl. the lock, a foreign PID and the launcher.
#
#   40_verify.sh [--quick] [REPO]      REPO defaults to $WORK_DIR/pluto-tx
#   40_verify.sh --dry-run-no-gnuradio only for trying these scripts on a machine
#                                      without GNU Radio: new tests may skip,
#                                      install-web-trx.sh --sim-only. NEVER on the radio server.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config

QUICK=0
ALLOW_NEW_SKIP=()
INSTALL_ARGS=()
R="$BUILD_REPO"
for arg in "${REST_ARGS[@]}"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --dry-run-no-gnuradio) ALLOW_NEW_SKIP=(--allow-new-skip); INSTALL_ARGS=(--sim-only) ;;
    -*) die "unbekannte Option $arg" ;;
    *) R="$arg" ;;
  esac
done
[[ "$QUICK" == 1 ]] || start_log 40_verify
[[ -d "$R/.git" ]] || die "$R ist kein Git-Checkout"
case "$R/" in "$PLUTO_TX_DIR"/) die "40_verify.sh prüft den Scratch-Klon, nicht den Produktiv-Checkout" ;; esac
BASE_SHA="$(cat "$WORK_DIR/base-sha" 2>/dev/null || git -C "$R" merge-base HEAD origin/main)"
cd "$R"
log "Prüfe $R ($(git rev-parse --short HEAD)) gegen Basis $(git rev-parse --short "$BASE_SHA")$([[ $QUICK == 1 ]] && echo ' [quick]')"

# --- quick -----------------------------------------------------------------
ALLOWED='^(web-trx/.*|\.github/workflows/web-trx\.yml|install-web-trx\.sh|pluto_tx/webtrx_(control|widget)\.py|pluto_tx/gui\.py|pluto_advanced_rx/gui\.py|tests/test_webtrx_[a-z_]+\.py|README\.md|merge web-trx/.*)$'
bad=0
while IFS=$'\t' read -r status path; do
  if ! [[ "$path" =~ $ALLOWED ]]; then
    fail "Änderung außerhalb der Liste: $status $path"; bad=1
  elif [[ "$status" == D && "$path" != "merge web-trx/"* ]]; then
    fail "gelöscht, aber nicht erlaubt: $path"; bad=1
  fi
done < <(git diff --no-renames --name-status "$BASE_SHA" HEAD)
[[ "$bad" == 0 ]] && ok "nur erlaubte Dateien geändert"

while read -r added deleted path; do
  [[ "$deleted" == 0 ]] && ok "$path: nur Einfügungen (+$added)" || fail "$path: $deleted Zeile(n) gelöscht"
done < <(git diff --numstat "$BASE_SHA" HEAD -- pluto_tx/gui.py pluto_advanced_rx/gui.py)

[[ -z "$(git ls-files -s | awk '$1 == "160000"')" ]] && ok "kein Gitlink (Submodule)" || fail "Gitlink vorhanden"
[[ ! -e .gitmodules && ! -e web-trx/.gitmodules ]] && ok "keine .gitmodules" || fail ".gitmodules vorhanden"

while IFS= read -r f; do
  bash -n "$f" && ok "bash -n $f" || fail "Syntaxfehler: $f"
done < <(git ls-files 'web-trx/scripts/*.sh' install-web-trx.sh)
while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  python3 -m py_compile "$f" && ok "py_compile $f" || fail "Python-Syntaxfehler: $f"
done < <(git diff --name-only --diff-filter=AM "$BASE_SHA" HEAD -- '*.py')
repo_is_clean . && ok "Arbeitsverzeichnis sauber" || fail "Arbeitsverzeichnis nicht sauber: $(git status --porcelain | head -5 | tr '\n' ' ')"

if [[ "$QUICK" == 1 ]]; then
  [[ "$FAILURES" == 0 ]] || die "Quick-Prüfung fehlgeschlagen ($FAILURES)"
  exit 0
fi

# --- full ------------------------------------------------------------------
echo
log "Import vollständig? (Blob-ID und Modus je Datei gegen Web-TRX $(cut -c1-9 "$WORK_DIR/import-sha"))"
SHA="$(cat "$WORK_DIR/import-sha")"
# files 30_build_merge.sh changes on purpose after the import
CHANGED_ON_PURPOSE='^(README\.md|backend/pyproject\.toml|backend/web_trx/(pluto_path|server|radio_backend|modes|protocol|session|sim_backend)\.py|backend/tests/(test_modes|test_server_ws)\.py|docs/(DEBUGGING|FT8_PLAN|PROJECT_PLAN)\.md|frontend/src/App\.svelte|scripts/(start|stop)\.sh)$'
declare -A NEW_TREE
while read -r mode _ blob path; do
  NEW_TREE["${path#web-trx/}"]="$mode $blob"
done < <(git ls-tree -r HEAD web-trx/)
missing=0; differ=0; purposely=0
while read -r mode type blob path; do
  case "$path" in vendor/pluto-tx|.gitmodules|LICENSE|.github/*) continue ;; esac
  [[ "$type" == blob ]] || continue
  now="${NEW_TREE[$path]:-}"
  if [[ -z "$now" ]]; then
    fail "fehlt nach dem Import: $path"; missing=$((missing + 1))
  elif [[ "$now" != "$mode $blob" ]]; then
    if [[ "$path" =~ $CHANGED_ON_PURPOSE && "${now%% *}" == "$mode" ]]; then
      purposely=$((purposely + 1))
    else
      fail "weicht ab: $path (vorher $mode $blob, jetzt $now)"; differ=$((differ + 1))
    fi
  fi
done < <(git -C "$WEB_TRX_OLD_DIR" ls-tree -r "$SHA")
[[ "$missing" == 0 && "$differ" == 0 ]] && ok "alle Dateien übernommen ($purposely davon absichtlich geändert)"

for p in web-trx/run/x web-trx/web-trx.env web-trx/backend/.venv/x web-trx/frontend/node_modules/x web-trx/frontend/dist/x; do
  git check-ignore -q "$p" && ok "ignoriert: $p" || fail "NICHT ignoriert: $p"
done

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck web-trx/scripts/*.sh install-web-trx.sh && ok "shellcheck" || fail "shellcheck meldet Probleme"
else
  warn "shellcheck nicht installiert -- übersprungen"
fi

echo
log "pluto-tx-Tests (python3 -m unittest discover tests)"
set +e
run_clean python3 "$MERGE_SCRIPTS_DIR/unittest_json.py" "$RESULTS_DIR/verify-pluto-tx.json" >"$RESULTS_DIR/verify-pluto-tx.txt" 2>&1
set -e
tail -n 3 "$RESULTS_DIR/verify-pluto-tx.txt"
if [[ -f "$RESULTS_DIR/baseline-pluto-tx.json" ]]; then
  python3 "$MERGE_SCRIPTS_DIR/compare_results.py" unittest "$RESULTS_DIR/baseline-pluto-tx.json" \
    "$RESULTS_DIR/verify-pluto-tx.json" "${ALLOW_NEW_SKIP[@]}" && ok "pluto-tx: keine Regression" || fail "pluto-tx: Regression (Details oben, Log $RESULTS_DIR/verify-pluto-tx.txt)"
else
  fail "keine Baseline ($RESULTS_DIR/baseline-pluto-tx.json) -- 10_baseline.sh zuerst"
fi

echo
log "install-web-trx.sh mit Wegwerf-HOME (echtes venv + Frontend im Scratch-Klon)"
FAKE_HOME="$WORK_DIR/fakehome"
rm -rf "$FAKE_HOME"; mkdir -p "$FAKE_HOME"
latest_backup="$(cat "$BACKUP_DIR/LATEST" 2>/dev/null || true)"
if [[ -n "$latest_backup" && -f "$latest_backup/old-venv-freeze.txt" ]]; then
  INSTALL_ARGS+=(--constraints "$latest_backup/old-venv-freeze.txt")
fi
if HOME="$FAKE_HOME" run_clean ./install-web-trx.sh "${INSTALL_ARGS[@]}" >"$RESULTS_DIR/verify-install.txt" 2>&1; then
  ok "install-web-trx.sh erfolgreich ($(grep -m1 '^OK' "$RESULTS_DIR/verify-install.txt" || echo 'kein OK?'))"
else
  fail "install-web-trx.sh fehlgeschlagen, Log: $RESULTS_DIR/verify-install.txt"; tail -n 15 "$RESULTS_DIR/verify-install.txt"
fi
LAUNCHER="$FAKE_HOME/.local/bin/web-trx"
[[ -x "$LAUNCHER" ]] && ok "Starter angelegt (Wegwerf-HOME)" || fail "Starter fehlt"
VENV_PY="$R/web-trx/backend/.venv/bin/python"

echo
log "web-trx: ruff + pytest (aus web-trx/backend)"
if [[ -x "$VENV_PY" ]]; then
  (cd web-trx/backend && run_clean .venv/bin/ruff check web_trx tests) && ok "ruff" || fail "ruff"
  set +e
  (cd web-trx/backend && run_clean .venv/bin/python -m pytest -q -p no:cacheprovider \
      --junitxml="$RESULTS_DIR/verify-web-trx.xml") >"$RESULTS_DIR/verify-web-trx.txt" 2>&1
  set -e
  tail -n 2 "$RESULTS_DIR/verify-web-trx.txt"
  if [[ -f "$RESULTS_DIR/baseline-web-trx.xml" ]]; then
    python3 "$MERGE_SCRIPTS_DIR/compare_results.py" junit "$RESULTS_DIR/baseline-web-trx.xml" "$RESULTS_DIR/verify-web-trx.xml" \
      --renamed test_tables_match_pinned_pluto_tx=test_tables_match_pluto_tx \
      --renamed test_rtty_and_digitext_tables_match_pinned_pluto_tx=test_rtty_and_digitext_tables_match_pluto_tx \
      --renamed test_ft8_rx_tables_match_pinned_pluto_tx=test_ft8_rx_tables_match_pluto_tx \
      "${ALLOW_NEW_SKIP[@]}" && ok "web-trx: keine Regression" || fail "web-trx: Regression"
  else
    grep -qE '[0-9]+ passed' "$RESULTS_DIR/verify-web-trx.txt" && ! grep -qE '[0-9]+ (failed|error)' "$RESULTS_DIR/verify-web-trx.txt" \
      && ok "web-trx pytest grün (keine Baseline zum Vergleich)" || fail "web-trx pytest rot"
  fi
  if (cd web-trx/frontend && npm run check >"$RESULTS_DIR/verify-frontend.txt" 2>&1); then ok "npm run check"; else fail "npm run check"; fi
else
  fail "kein venv -- pytest/ruff übersprungen"
fi

echo
log "Script-Smoke (sim, http://127.0.0.1:$SMOKE_PORT)"
if command -v ss >/dev/null 2>&1 && ss -Htln "( sport = :$SMOKE_PORT )" | grep -q .; then
  fail "Port $SMOKE_PORT belegt -- Smoke übersprungen"
elif [[ ! -x "$LAUNCHER" ]]; then
  fail "kein Starter -- Smoke übersprungen"
else
  W="$R/web-trx"
  cat >"$W/web-trx.env" <<EOF
WEB_TRX_BACKEND=sim
WEB_TRX_HOST=127.0.0.1
WEB_TRX_PORT=$SMOKE_PORT
WEB_TRX_TLS=false
WEB_TRX_PASSWORD=smoke
EOF
  wt() { HOME="$FAKE_HOME" run_clean "$LAUNCHER" "$@"; }
  expect() {  # expect CODE DESCRIPTION CMD...
    local want="$1" what="$2" rc=0; shift 2
    "$@" >>"$RESULTS_DIR/verify-smoke.txt" 2>&1 || rc=$?
    [[ "$rc" == "$want" ]] && ok "$what (Exit $rc)" || fail "$what: Exit $rc, erwartet $want"
  }
  uvicorns() { pgrep -f "$W/backend/.venv/bin/uvicorn" | wc -l; }
  : >"$RESULTS_DIR/verify-smoke.txt"
  expect 3 "status vor dem Start: gestoppt" wt status
  expect 0 "start" wt start
  expect 0 "status: läuft" wt status
  json="$(wt status --json || true)"
  python3 - "$json" <<'EOF' && ok "status --json: running, SimBackend, devices" || fail "status --json unerwartet: $json"
import json, sys
d = json.loads(sys.argv[1])
assert d["state"] == "running" and d["backend"] == "SimBackend", d
assert set(d["devices"]) == {"rx", "tx"}, d
EOF
  expect 3 "zweiter start: läuft bereits" wt start
  python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$SMOKE_PORT/session', timeout=3).read()" \
    && ok "GET /session" || fail "GET /session"
  sleep 1
  if grep -q '"GET /session' "$W/run/web-trx.log" && ! grep -q '"GET /health' "$W/run/web-trx.log"; then
    ok "Access-Log: /session drin, /health nicht"
  else
    fail "Access-Log unerwartet (siehe $W/run/web-trx.log)"
  fi
  expect 0 "restart" wt restart
  [[ "$(uvicorns)" == 1 ]] && ok "genau ein Server nach restart" || fail "$(uvicorns) Server-Prozesse nach restart"
  expect 0 "stop" wt stop
  expect 3 "status nach stop: gestoppt" wt status
  [[ "$(uvicorns)" == 0 ]] && ok "kein Server-Prozess mehr" || fail "$(uvicorns) Server-Prozesse übrig"

  # two starts at the same time: the lock lets exactly one server through
  rc1=0; rc2=0
  wt start >>"$RESULTS_DIR/verify-smoke.txt" 2>&1 & p1=$!
  wt start >>"$RESULTS_DIR/verify-smoke.txt" 2>&1 & p2=$!
  wait "$p1" || rc1=$?
  wait "$p2" || rc2=$?
  if [[ "$(printf '%s\n' "$rc1" "$rc2" | sort | tr '\n' ' ')" == "0 3 " && "$(uvicorns)" == 1 ]]; then
    ok "zwei gleichzeitige Starts: einer startet, einer meldet 'läuft bereits'"
  else
    fail "gleichzeitige Starts: Exit $rc1/$rc2, $(uvicorns) Server"
  fi
  [[ "$(cat "$W/run/web-trx.pid")" == "$(pgrep -f "$W/backend/.venv/bin/uvicorn")" ]] && ok "PID-Datei zeigt auf den Server" || fail "PID-Datei falsch"
  expect 0 "stop" wt stop

  # a stale PID file pointing at an unrelated process: stop must not touch it
  sleep 300 & foreign=$!
  echo "$foreign" >"$W/run/web-trx.pid"
  expect 0 "stop mit fremder PID in der PID-Datei" wt stop
  if kill -0 "$foreign" 2>/dev/null; then ok "fremder Prozess lebt noch"; else fail "fremder Prozess wurde beendet!"; fi
  kill "$foreign" 2>/dev/null || true
  wait "$foreign" 2>/dev/null || true
  [[ ! -e "$W/run/web-trx.pid" ]] && ok "veraltete PID-Datei entfernt" || fail "PID-Datei noch da"

  url="$(run_clean python3 -m pluto_tx.webtrx_control url)"
  [[ "$url" == "http://localhost:$SMOKE_PORT" ]] && ok "webtrx_control url: $url" || fail "url: $url"
  rc=0; HOME="$FAKE_HOME" timeout 2 "$LAUNCHER" log >/dev/null 2>&1 || rc=$?
  [[ "$rc" == 124 ]] && ok "web-trx log (tail -F, per timeout beendet)" || fail "web-trx log: Exit $rc"
  expect 64 "web-trx unbekannt -> Usage" wt nonsense
  rm -f "$W/web-trx.env"
fi

echo
repo_is_clean . && ok "Arbeitsverzeichnis nach allem sauber (Ignorierregeln greifen)" || fail "Arbeitsverzeichnis nicht sauber: $(git status --porcelain | head -5 | tr '\n' ' ')"
{
  echo "verify $(date -Is) on $(git rev-parse HEAD): $FAILURES failure(s)"
} >>"$RESULTS_DIR/verify-summary.txt"
summary_exit
