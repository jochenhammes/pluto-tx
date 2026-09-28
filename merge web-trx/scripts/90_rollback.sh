#!/usr/bin/env bash
# M9 -- back to the old operation: Web-TRX from ~/Dokumente/Web-TRX.
# Never uses reset or force-push. Each step only with its option:
#
#   90_rollback.sh --stop-new             stop the merged server (web-trx/scripts/stop.sh)
#   90_rollback.sh --copy-back-runtime    copy settings/sessions/TX log that the NEW server
#                                         wrote back to the old checkout (old files -> backup)
#   90_rollback.sh --start-old            start the old server ($WEB_TRX_OLD_DIR/scripts/start.sh)
#   90_rollback.sh --remove-launcher      move ~/.local/bin/web-trx into the backup
#   90_rollback.sh --revert-code          git revert of the merge commits on main (local
#                                         commits only; push is a separate, manual step)
#   90_rollback.sh --all                  the first four (not --revert-code)
#
# Limitation: the old server imports the live pluto-tx checkout
# ($PLUTO_TX_DIR). That works as long as nobody changed the pluto_tx /
# pluto_advanced_rx interfaces after the merge (MERGE_PLAN.md M9).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config
start_log 90_rollback

STOP_NEW=0; COPY_BACK=0; START_OLD=0; RM_LAUNCHER=0; REVERT=0
for arg in "${REST_ARGS[@]}"; do
  case "$arg" in
    --stop-new) STOP_NEW=1 ;;
    --copy-back-runtime) COPY_BACK=1 ;;
    --start-old) START_OLD=1 ;;
    --remove-launcher) RM_LAUNCHER=1 ;;
    --revert-code) REVERT=1 ;;
    --all) STOP_NEW=1; COPY_BACK=1; START_OLD=1; RM_LAUNCHER=1 ;;
    *) die "unbekannte Option $arg" ;;
  esac
done
[[ $((STOP_NEW + COPY_BACK + START_OLD + RM_LAUNCHER + REVERT)) -gt 0 ]] || die "keine Aktion gewählt (siehe Kopf dieses Skripts)"

NEW="$PLUTO_TX_DIR/web-trx"
SAVE="$BACKUP_DIR/rollback-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SAVE"

if [[ "$STOP_NEW" == 1 ]]; then
  if pid="$(webtrx_pid_alive "$NEW")"; then
    confirm "Neuen Web-TRX-Server (PID $pid) stoppen?"
    rc=0; "$NEW/scripts/stop.sh" || rc=$?
    [[ "$rc" == 2 ]] && warn "Server musste hart beendet werden -- Safe-State des Senders prüfen!"
    [[ "$rc" == 0 || "$rc" == 2 ]] || die "stop.sh Exit $rc"
  else
    ok "neuer Server läuft nicht"
  fi
fi

if [[ "$COPY_BACK" == 1 ]]; then
  if webtrx_pid_alive "$NEW" >/dev/null; then die "neuer Server läuft noch -- zuerst --stop-new"; fi
  if webtrx_pid_alive "$WEB_TRX_OLD_DIR" >/dev/null; then die "alter Server läuft -- erst stoppen"; fi
  confirm "Laufzeitdaten des neuen Servers in den alten Checkout zurückkopieren (alte Dateien werden nach $SAVE gesichert)?"
  for item in run/settings.json run/sessions.json run/web_trx_tx_log.sqlite3; do
    [[ -e "$NEW/$item" ]] || continue
    if [[ -e "$WEB_TRX_OLD_DIR/$item" ]]; then
      mkdir -p "$SAVE/old/$(dirname "$item")"
      cp -p "$WEB_TRX_OLD_DIR/$item" "$SAVE/old/$item"
    fi
    mkdir -p "$WEB_TRX_OLD_DIR/$(dirname "$item")"
    cp -p "$NEW/$item" "$WEB_TRX_OLD_DIR/$item"
    ok "$item zurückkopiert"
  done
fi

if [[ "$START_OLD" == 1 ]]; then
  if webtrx_pid_alive "$NEW" >/dev/null; then die "neuer Server läuft noch -- zuerst --stop-new"; fi
  [[ -x "$WEB_TRX_OLD_DIR/scripts/start.sh" ]] || die "$WEB_TRX_OLD_DIR/scripts/start.sh fehlt"
  confirm "Alten Web-TRX-Server aus $WEB_TRX_OLD_DIR starten?"
  "$WEB_TRX_OLD_DIR/scripts/start.sh"
fi

if [[ "$RM_LAUNCHER" == 1 ]]; then
  if [[ -e "$HOME/.local/bin/web-trx" ]]; then
    mv "$HOME/.local/bin/web-trx" "$SAVE/web-trx.launcher"
    ok "Starter web-trx nach $SAVE verschoben"
  else
    ok "kein Starter web-trx vorhanden"
  fi
fi

if [[ "$REVERT" == 1 ]]; then
  if webtrx_pid_alive "$NEW" >/dev/null; then die "neuer Server läuft noch -- zuerst --stop-new"; fi
  repo_is_clean "$PLUTO_TX_DIR" || die "$PLUTO_TX_DIR nicht sauber"
  base="$(git -C "$PLUTO_TX_DIR" rev-parse -q --verify refs/tags/pre-web-trx-merge)" || die "Tag pre-web-trx-merge fehlt"
  log "Merge-Commits seit pre-web-trx-merge:"
  git -C "$PLUTO_TX_DIR" log --oneline "$base..HEAD"
  confirm "Diese Commits auf main per 'git revert' rückgängig machen (lokale neue Commits, kein Push)?"
  git -C "$PLUTO_TX_DIR" revert --no-edit "$base..HEAD"
  # what is left of web-trx/ now is only ignored runtime data (venv,
  # node_modules, run/, web-trx.env) -- untracked after the revert
  if [[ -d "$NEW" && -z "$(git -C "$PLUTO_TX_DIR" ls-files web-trx)" ]]; then
    mv "$NEW" "$SAVE/web-trx-leftovers"
    ok "Reste von web-trx/ (Laufzeitdaten, venv) nach $SAVE/web-trx-leftovers verschoben"
  fi
  repo_is_clean "$PLUTO_TX_DIR" && ok "Checkout sauber" || warn "Checkout nicht sauber: git -C \"$PLUTO_TX_DIR\" status"
  ok "Revert-Commits erstellt. Push erst nach Rücksprache: git -C \"$PLUTO_TX_DIR\" push origin main"
fi
log "Rollback-Schritte fertig. Sicherungen: $SAVE"
