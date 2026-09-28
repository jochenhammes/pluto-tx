#!/usr/bin/env bash
# M2 -- Backup, everything OUTSIDE the repositories, in $BACKUP_DIR/<time>/:
#   pluto-tx.bundle, web-trx.bundle   complete git history incl. all branches
#   runtime.tgz                       web-trx.env + run/ of the old Web-TRX
#   launchers/                        ~/.local/bin/pluto-tx, -advanced-rx, -cli (+ web-trx if any)
#   old-venv-freeze.txt               pip freeze of the old venv (constraints for M6)
#   SHA256SUMS
# plus a local tag "pre-web-trx-merge" at pluto-tx HEAD (not pushed), and a
# copy of "merge web-trx/" itself in $MERGE_HOME/tools (the merge deletes the
# folder from the checkout; M5-M9 run from the copy).
#
#   20_backup.sh               first backup (M2), the old server may still run
#   20_backup.sh --after-stop  second backup of the runtime data in M5, after
#                              the old server is stopped (consistent SQLite)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config
start_log 20_backup

AFTER_STOP=0
for arg in "${REST_ARGS[@]}"; do
  case "$arg" in
    --after-stop) AFTER_STOP=1 ;;
    *) die "unbekannte Option $arg" ;;
  esac
done

DEST="$BACKUP_DIR/$(date +%Y%m%d-%H%M%S)"
[[ "$AFTER_STOP" == 1 ]] && DEST="$DEST-after-stop"
mkdir -p "$DEST"
log "Ziel: $DEST"

backup_runtime() {
  local items=() f
  for f in web-trx.env run; do
    [[ -e "$WEB_TRX_OLD_DIR/$f" ]] && items+=("$f")
  done
  if [[ ${#items[@]} -eq 0 ]]; then
    warn "keine Laufzeitdaten in $WEB_TRX_OLD_DIR"
    return
  fi
  tar -C "$WEB_TRX_OLD_DIR" -czpf "$DEST/runtime.tgz" "${items[@]}"
  tar -tzf "$DEST/runtime.tgz" >/dev/null
  ok "runtime.tgz: ${items[*]}"
  # the individual files as well, for the sha comparison in M6
  mkdir -p "$DEST/runtime"
  tar -C "$DEST/runtime" -xzpf "$DEST/runtime.tgz"
}

if [[ "$AFTER_STOP" == 1 ]]; then
  if pid="$(webtrx_pid_alive "$WEB_TRX_OLD_DIR")"; then
    die "der alte Web-TRX-Server läuft noch (PID $pid) -- erst $WEB_TRX_OLD_DIR/scripts/stop.sh"
  fi
  for f in "$WEB_TRX_OLD_DIR"/run/*-journal "$WEB_TRX_OLD_DIR"/run/*-wal; do
    [[ -e "$f" ]] && die "SQLite-Journal $f vorhanden -- Datenbank nicht sauber geschlossen. Betreiber fragen (MERGE_PLAN.md M5)."
  done
  backup_runtime
  echo "$DEST" >"$BACKUP_DIR/LATEST_AFTER_STOP"
else
  log "Git-Bundles"
  git -C "$PLUTO_TX_DIR" bundle create "$DEST/pluto-tx.bundle" --all
  git bundle verify -q "$DEST/pluto-tx.bundle" && ok "pluto-tx.bundle geprüft"
  git -C "$WEB_TRX_OLD_DIR" bundle create "$DEST/web-trx.bundle" --all
  git bundle verify -q "$DEST/web-trx.bundle" && ok "web-trx.bundle geprüft"

  head="$(git -C "$PLUTO_TX_DIR" rev-parse HEAD)"
  if existing="$(git -C "$PLUTO_TX_DIR" rev-parse -q --verify refs/tags/pre-web-trx-merge 2>/dev/null)"; then
    [[ "$existing" == "$head" ]] && ok "Tag pre-web-trx-merge existiert schon auf HEAD" \
      || die "Tag pre-web-trx-merge zeigt auf $existing, HEAD ist $head -- Betreiber fragen"
  else
    git -C "$PLUTO_TX_DIR" tag pre-web-trx-merge "$head"
    ok "lokaler Tag pre-web-trx-merge -> $head"
  fi
  echo "PLUTO_TX_HEAD=$head" >"$DEST/HEADS"
  echo "WEB_TRX_HEAD=$(git -C "$WEB_TRX_OLD_DIR" rev-parse HEAD)" >>"$DEST/HEADS"

  backup_runtime

  mkdir -p "$DEST/launchers"
  for launcher in pluto-tx pluto-advanced-rx pluto-cli web-trx; do
    [[ -e "$HOME/.local/bin/$launcher" ]] && cp -p "$HOME/.local/bin/$launcher" "$DEST/launchers/" && ok "Starter $launcher"
  done

  if [[ -x "$WEB_TRX_OLD_DIR/backend/.venv/bin/pip" ]]; then
    "$WEB_TRX_OLD_DIR/backend/.venv/bin/pip" freeze >"$DEST/old-venv-freeze.txt"
    ok "old-venv-freeze.txt ($(wc -l <"$DEST/old-venv-freeze.txt") Pakete)"
  fi
  echo "$DEST" >"$BACKUP_DIR/LATEST"

  # The merge removes "merge web-trx/" from the checkout (commit 8): from M5
  # on, the scripts run from this copy outside the repository.
  TOOLS="$MERGE_HOME/tools"
  if [[ "$MERGE_DIR" != "$TOOLS" ]]; then
    rm -rf "$TOOLS.new"
    cp -a "$MERGE_DIR" "$TOOLS.new"
    rm -rf "$TOOLS"
    mv "$TOOLS.new" "$TOOLS"
    ok "Merge-Werkzeuge kopiert nach $TOOLS (ab M5 von dort aufrufen)"
  fi
fi

(cd "$DEST" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >"$DEST.sums.tmp" && mv "$DEST.sums.tmp" SHA256SUMS)
ok "SHA256SUMS ($(wc -l <"$DEST/SHA256SUMS") Dateien)"
du -sh "$DEST"
log "Backup fertig: $DEST"
