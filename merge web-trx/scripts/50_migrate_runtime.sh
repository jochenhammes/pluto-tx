#!/usr/bin/env bash
# M6 -- copies the runtime data of the old Web-TRX checkout into the merged
# one (BEFORE install-web-trx.sh and the first start). Copies, never moves:
# the old checkout stays usable for a rollback.
#
#   web-trx.env, run/settings.json, run/sessions.json,
#   run/web_trx_tx_log.sqlite3, run/tls/cert.pem, run/tls/key.pem
#   (not: web-trx.log, web-trx.pid)
#
#   50_migrate_runtime.sh                       dry run: shows what would happen
#   50_migrate_runtime.sh --apply               copy (refuses to overwrite a different file)
#   50_migrate_runtime.sh --apply --force       overwrite differing files (old target -> backup)
#   ... --rewrite-paths                         comment out *_PATH lines in web-trx.env
#                                               that point into the old checkout
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config
start_log 50_migrate_runtime

APPLY=0; FORCE=0; REWRITE=0
for arg in "${REST_ARGS[@]}"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    --rewrite-paths) REWRITE=1 ;;
    *) die "unbekannte Option $arg" ;;
  esac
done

SRC="$WEB_TRX_OLD_DIR"
DST="$PLUTO_TX_DIR/web-trx"
[[ -d "$DST/backend" ]] || die "$DST fehlt -- erst M5 (Merge nach main, git pull --ff-only)"
if pid="$(webtrx_pid_alive "$SRC")"; then die "alter Server läuft noch (PID $pid) -- $SRC/scripts/stop.sh"; fi
if pid="$(webtrx_pid_alive "$DST")"; then die "neuer Server läuft schon (PID $pid) -- Migration nur vor dem ersten Start"; fi
for f in "$SRC"/run/*-journal "$SRC"/run/*-wal; do
  [[ -e "$f" ]] && die "SQLite-Journal $f vorhanden -- Datenbank nicht sauber geschlossen (MERGE_PLAN.md M5)"
done

ITEMS=(web-trx.env run/settings.json run/sessions.json run/web_trx_tx_log.sqlite3 run/tls/cert.pem run/tls/key.pem)
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# *_PATH settings in web-trx.env that point into the old checkout
ENV_PATH_LINES="$(grep -nE '^[[:space:]]*(export[[:space:]]+)?WEB_TRX_[A-Z_]*PATH=' "$SRC/web-trx.env" 2>/dev/null || true)"
STALE_PATHS=""
if [[ -n "$ENV_PATH_LINES" ]]; then
  log "Pfad-Einstellungen in web-trx.env:"
  printf '    %s\n' "$ENV_PATH_LINES"
  STALE_PATHS="$(printf '%s\n' "$ENV_PATH_LINES" | grep -E "Web-TRX|$(basename "$SRC")|WEB_TRX_PLUTO_TX_PATH" || true)"
  if [[ -n "$STALE_PATHS" ]]; then
    warn "diese Zeilen zeigen in den alten Checkout bzw. setzen den pluto-tx-Pfad (jetzt Standard: das Repo selbst):"
    printf '    %s\n' "$STALE_PATHS"
    [[ "$REWRITE" == 1 ]] || warn "mit --rewrite-paths werden sie in der Kopie auskommentiert (Betreiber fragen)"
  fi
fi

# expected content of each target file: the source, except web-trx.env with
# --rewrite-paths (those lines commented out)
expected() {
  if [[ "$1" == web-trx.env && "$REWRITE" == 1 && -n "$STALE_PATHS" ]]; then
    local out="$TMP/web-trx.env" lineno
    cp "$SRC/web-trx.env" "$out"
    while IFS=: read -r lineno _; do
      sed -i "${lineno}s|^|# (vor Zusammenführung, auskommentiert) |" "$out"
    done <<<"$STALE_PATHS"
    echo "$out"
  else
    echo "$SRC/$1"
  fi
}

PLAN_COPY=(); CONFLICTS=()
log "$([[ $APPLY == 1 ]] && echo AUSFÜHRUNG || echo TROCKENLAUF): $SRC -> $DST"
for item in "${ITEMS[@]}"; do
  if [[ ! -e "$SRC/$item" ]]; then
    ok "$item: im alten Checkout nicht vorhanden -- nichts zu tun"
    continue
  fi
  want="$(expected "$item")"
  if [[ ! -e "$DST/$item" ]]; then
    ok "$item: wird kopiert$([[ "$want" != "$SRC/$item" ]] && echo ' (mit auskommentierten Pfaden)')"
    PLAN_COPY+=("$item")
  elif cmp -s "$want" "$DST/$item"; then
    ok "$item: schon wie erwartet"
  else
    warn "$item: existiert im Ziel mit anderem Inhalt"
    CONFLICTS+=("$item")
  fi
done

if [[ ${#CONFLICTS[@]} -gt 0 && "$FORCE" != 1 ]]; then
  [[ "$APPLY" == 1 ]] && die "Konflikte: ${CONFLICTS[*]} -- Betreiber fragen; mit --force überschreiben (Ziel wird vorher gesichert)"
  warn "Konflikte: ${CONFLICTS[*]} (bei --apply ohne --force: Abbruch)"
fi
if [[ -n "$STALE_PATHS" && "$REWRITE" != 1 && "$APPLY" == 1 ]]; then
  die "web-trx.env enthält Pfade in den alten Checkout -- mit --rewrite-paths erneut (nach Rückfrage beim Betreiber)"
fi

if [[ "$APPLY" != 1 ]]; then
  log "Trockenlauf beendet -- nichts verändert. Ausführen mit --apply."
  exit 0
fi

if [[ ${#CONFLICTS[@]} -gt 0 ]]; then
  SAVE="$BACKUP_DIR/migrate-overwritten-$(date +%Y%m%d-%H%M%S)"
  for item in "${CONFLICTS[@]}"; do
    mkdir -p "$SAVE/$(dirname "$item")"
    cp -p "$DST/$item" "$SAVE/$item"
    PLAN_COPY+=("$item")
  done
  ok "überschriebene Zieldateien gesichert: $SAVE"
fi

mkdir -p "$DST/run"
for item in "${PLAN_COPY[@]}"; do
  if [[ "$item" == run/tls/* ]]; then
    mkdir -p "$DST/run/tls"
    chmod 700 "$DST/run/tls"
  fi
  want="$(expected "$item")"
  if [[ "$want" == "$SRC/$item" ]]; then
    cp -p "$SRC/$item" "$DST/$item"
  else
    cp "$want" "$DST/$item"
    touch -r "$SRC/$item" "$DST/$item"
  fi
  [[ "$item" == run/tls/key.pem || "$item" == web-trx.env ]] && chmod 600 "$DST/$item"
done

log "Prüfsummen (Ziel gegen erwarteten Inhalt)"
for item in "${ITEMS[@]}"; do
  [[ -e "$SRC/$item" ]] || continue
  want="$(expected "$item")"
  a="$(sha256sum <"$want")"; b="$(sha256sum <"$DST/$item")"
  if [[ "$a" == "$b" ]]; then
    ok "$item identisch$([[ "$want" != "$SRC/$item" ]] && echo ' (Pfade auskommentiert; Original bleibt im alten Checkout und im Backup)')"
  else
    fail "$item weicht ab"
  fi
done
[[ "$(stat -c %a "$DST/run/tls/key.pem" 2>/dev/null || echo 600)" == 600 ]] && ok "key.pem Modus 600" || fail "key.pem Modus falsch"
if git -C "$PLUTO_TX_DIR" status --porcelain | grep -q .; then
  fail "git status nicht sauber: $(git -C "$PLUTO_TX_DIR" status --porcelain | head -3 | tr '\n' ' ')"
else
  ok "git status sauber (Laufzeitdaten ignoriert)"
fi
summary_exit
