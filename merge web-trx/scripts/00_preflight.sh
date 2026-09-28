#!/usr/bin/env bash
# M0 -- Preflight. ONLY READS (one exception: `git fetch` in both repos to
# compare with GitHub; it changes no branch, no working tree). Run it as often
# as you like. Exit code 0 = everything needed for M1-M4 is in place.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config
start_log 00_preflight

log "Konfiguration: ${MERGE_ENV} $( [[ -f "$MERGE_ENV" ]] && echo '(gelesen)' || echo '(nicht vorhanden, Standardwerte)')"
log "  PLUTO_TX_DIR=$PLUTO_TX_DIR"
log "  WEB_TRX_OLD_DIR=$WEB_TRX_OLD_DIR"
log "  MERGE_HOME=$MERGE_HOME  (WORK_DIR=$WORK_DIR, BACKUP_DIR=$BACKUP_DIR)"
echo

log "1) Werkzeuge"
for tool in git python3 tar sha256sum flock bash grep sed awk; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool"; else fail "$tool fehlt"; fi
done
for tool in node npm openssl ss shellcheck; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool"; else warn "$tool fehlt (siehe MERGE_PLAN.md, was davon abhängt)"; fi
done
if command -v node >/dev/null 2>&1; then
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [[ "$node_major" -ge 18 ]]; then ok "Node.js $(node --version)"; else warn "Node.js $(node --version) < 18: Frontend-Build nur mit --reuse-dist"; fi
fi
if python3 -c 'import gnuradio, iio, PyQt5' 2>/dev/null; then ok "python3: gnuradio, iio, PyQt5 importierbar"; else fail "python3 kann gnuradio/iio/PyQt5 nicht importieren"; fi

echo
log "2) pluto-tx ($PLUTO_TX_DIR)"
if git -C "$PLUTO_TX_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  url="$(git -C "$PLUTO_TX_DIR" remote get-url origin 2>/dev/null || true)"
  [[ "$url" == *"$PLUTO_TX_REMOTE_MATCH"* ]] && ok "origin = $url" || fail "origin ist '$url', erwartet *$PLUTO_TX_REMOTE_MATCH*"
  branch="$(git -C "$PLUTO_TX_DIR" symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
  [[ "$branch" == main ]] && ok "Branch main" || fail "Branch ist '$branch', erwartet main"
  repo_is_clean "$PLUTO_TX_DIR" && ok "Arbeitsverzeichnis sauber" || fail "nicht committete/ungetrackte Dateien: git -C \"$PLUTO_TX_DIR\" status"
  if git -C "$PLUTO_TX_DIR" fetch -q origin main 2>/dev/null; then
    head="$(git -C "$PLUTO_TX_DIR" rev-parse HEAD)"; remote="$(git -C "$PLUTO_TX_DIR" rev-parse origin/main)"
    [[ "$head" == "$remote" ]] && ok "HEAD = origin/main ($head)" || fail "HEAD $head != origin/main $remote (pull/push zuerst)"
  else
    fail "git fetch fehlgeschlagen (Netz?)"
  fi
  [[ -d "$PLUTO_TX_DIR/merge web-trx" ]] && ok "Entwürfe 'merge web-trx/' vorhanden" || fail "'merge web-trx/' fehlt im Checkout"
  [[ ! -e "$PLUTO_TX_DIR/web-trx" ]] && ok "web-trx/ existiert noch nicht" || fail "$PLUTO_TX_DIR/web-trx existiert schon"
  [[ ! -e "$PLUTO_TX_DIR/install-web-trx.sh" ]] && ok "install-web-trx.sh existiert noch nicht" || fail "install-web-trx.sh existiert schon"
  if [[ -f "$MERGE_DIR/BASE" ]]; then
    drafted_on="$(awk -F= '$1=="PLUTO_TX_BASE"{print $2}' "$MERGE_DIR/BASE")"
    if [[ -n "$drafted_on" ]] && git -C "$PLUTO_TX_DIR" diff --quiet "$drafted_on" HEAD -- pluto_tx/gui.py pluto_advanced_rx/gui.py README.md 2>/dev/null; then
      ok "gui.py (TX/RX) und README.md unverändert seit dem Entwurf ($drafted_on)"
    else
      warn "gui.py/README.md haben sich seit dem Entwurf ($drafted_on) geändert -- Patches brauchen evtl. 3-Wege-Anwendung (30_build_merge.sh versucht das; sonst patches/README.md)"
    fi
  fi
else
  fail "$PLUTO_TX_DIR ist kein Git-Repository"
fi

echo
log "3) Web-TRX alt ($WEB_TRX_OLD_DIR)"
if git -C "$WEB_TRX_OLD_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  url="$(git -C "$WEB_TRX_OLD_DIR" remote get-url origin 2>/dev/null || true)"
  [[ "$url" == *"$WEB_TRX_REMOTE_MATCH"* ]] && ok "origin = $url" || fail "origin ist '$url', erwartet *$WEB_TRX_REMOTE_MATCH*"
  branch="$(git -C "$WEB_TRX_OLD_DIR" symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
  [[ "$branch" == main ]] && ok "Branch main" || fail "Branch ist '$branch', erwartet main"
  repo_is_clean "$WEB_TRX_OLD_DIR" && ok "Arbeitsverzeichnis sauber" || fail "nicht committete Änderungen: git -C \"$WEB_TRX_OLD_DIR\" status"
  if git -C "$WEB_TRX_OLD_DIR" fetch -q origin main 2>/dev/null; then
    head="$(git -C "$WEB_TRX_OLD_DIR" rev-parse HEAD)"; remote="$(git -C "$WEB_TRX_OLD_DIR" rev-parse origin/main)"
    [[ "$head" == "$remote" ]] && ok "HEAD = origin/main ($head)" || fail "HEAD $head != origin/main $remote"
  else
    fail "git fetch fehlgeschlagen (Netz?)"
  fi
  if sha="$(import_sha 2>/dev/null)"; then ok "Import-Stand: $sha $(git -C "$WEB_TRX_OLD_DIR" log -1 --format='(%s)' "$sha")"; else fail "WEB_TRX_SHA '$WEB_TRX_SHA' unbekannt"; fi
  if [[ -f "$MERGE_DIR/BASE" ]]; then
    drafted_web="$(awk -F= '$1=="WEB_TRX_BASE"{print $2}' "$MERGE_DIR/BASE")"
    if [[ -n "$drafted_web" && -n "${sha:-}" ]] && git -C "$WEB_TRX_OLD_DIR" diff --quiet "$drafted_web" "$sha" 2>/dev/null; then
      ok "Web-TRX unverändert seit dem Entwurf ($drafted_web)"
    else
      warn "Web-TRX hat seit dem Entwurf ($drafted_web) neue Commits -- Patches werden 3-Wege angewendet, Ergebnis in M3 genau prüfen"
    fi
  fi
  [[ -x "$WEB_TRX_OLD_DIR/backend/.venv/bin/python" ]] && ok "altes venv vorhanden" || warn "altes venv fehlt (Baseline-Tests des alten Backends entfallen)"
  if grep -qiE '^include-system-site-packages *= *true' "$WEB_TRX_OLD_DIR/backend/.venv/pyvenv.cfg" 2>/dev/null; then ok "altes venv mit system-site-packages"; else warn "altes venv ohne system-site-packages?"; fi
else
  fail "$WEB_TRX_OLD_DIR ist kein Git-Repository"
fi

echo
log "4) Laufzeitdaten alt (werden in M6 kopiert, nie verschoben)"
for f in web-trx.env run/settings.json run/sessions.json run/web_trx_tx_log.sqlite3 run/tls/cert.pem run/tls/key.pem; do
  if [[ -e "$WEB_TRX_OLD_DIR/$f" ]]; then ok "$f ($(stat -c '%s Bytes, Modus %a' "$WEB_TRX_OLD_DIR/$f"))"; else warn "$f fehlt (wird dann nicht übernommen)"; fi
done
for f in "$WEB_TRX_OLD_DIR"/run/*-journal "$WEB_TRX_OLD_DIR"/run/*-wal; do
  [[ -e "$f" ]] && warn "SQLite-Journal $f vorhanden (Server läuft oder wurde hart beendet)"
done

echo
log "5) Laufende Programme"
if pgrep -af '[p]luto_tx\.app|[p]luto_advanced_rx\.app|[p]luto_cli\.app' >/dev/null; then
  fail "pluto-tx / pluto-advanced-rx / pluto-cli laufen noch:"; pgrep -af '[p]luto_tx\.app|[p]luto_advanced_rx\.app|[p]luto_cli\.app' || true
else
  ok "keine pluto-tx-App und kein pluto-cli aktiv"
fi
if pid="$(webtrx_pid_alive "$WEB_TRX_OLD_DIR")"; then
  warn "alter Web-TRX-Server läuft (PID $pid) -- in Ordnung bis M5, dort wird er gestoppt"
else
  ok "alter Web-TRX-Server läuft nicht"
fi
conns="$(established_on_port "$PROD_PORT")"
[[ "$conns" == 0 ]] && ok "keine Browser-Verbindung auf Port $PROD_PORT" || warn "$conns Verbindung(en) auf Port $PROD_PORT (vor M5 schließen)"
if ! command -v ss >/dev/null 2>&1; then
  warn "ss fehlt -- Port $SMOKE_PORT (Smoke-Test) nicht prüfbar"
elif ss -Htln "( sport = :$SMOKE_PORT )" 2>/dev/null | grep -q .; then
  fail "Port $SMOKE_PORT (Smoke-Test) ist belegt -- SMOKE_PORT in merge.env ändern"
else
  ok "Port $SMOKE_PORT frei für den Smoke-Test"
fi

echo
log "6) Umgebung"
if compgen -e | grep -q '^WEB_TRX_'; then
  fail "WEB_TRX_*-Variablen in dieser Shell gesetzt: $(compgen -e | grep '^WEB_TRX_' | tr '\n' ' ') -- in neuer Shell ohne sie arbeiten"
else
  ok "keine WEB_TRX_*-Variablen gesetzt"
fi
[[ -z "${PLUTO_WEBTRX_CONTROL:-}" ]] && ok "PLUTO_WEBTRX_CONTROL nicht gesetzt" || warn "PLUTO_WEBTRX_CONTROL=$PLUTO_WEBTRX_CONTROL gesetzt"
mkdir_parent="$(dirname "$MERGE_HOME")"
[[ -w "$mkdir_parent" ]] && ok "$mkdir_parent beschreibbar" || fail "$mkdir_parent nicht beschreibbar"
avail_kb="$(df -Pk "$mkdir_parent" | awk 'NR==2{print $4}')"
if [[ "$avail_kb" -ge 3000000 ]]; then ok "$((avail_kb / 1024)) MB frei"; else fail "nur $((avail_kb / 1024)) MB frei (mindestens 3 GB für Klone, venv, node_modules)"; fi
for launcher in pluto-tx pluto-advanced-rx pluto-cli web-trx; do
  if [[ -e "$HOME/.local/bin/$launcher" ]]; then ok "Starter ~/.local/bin/$launcher vorhanden"; else
    [[ "$launcher" == web-trx ]] && ok "Starter web-trx existiert noch nicht" || warn "Starter ~/.local/bin/$launcher fehlt"
  fi
done

summary_exit
