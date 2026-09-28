# shellcheck shell=bash
# Shared helpers for the merge scripts in "merge web-trx/scripts/". Sourced,
# never executed. Every script: set -euo pipefail, refuses root, loads the
# configuration, logs to $LOG_DIR.

MERGE_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGE_DIR="$(cd "$MERGE_SCRIPTS_DIR/.." && pwd)"           # ".../merge web-trx"
# shellcheck disable=SC2034  # used by the scripts that source this file
FILES_DIR="$MERGE_DIR/files"
# shellcheck disable=SC2034
PATCH_DIR="$MERGE_DIR/patches"
ASSUME_YES="${ASSUME_YES:-0}"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*" >&2; }
fail() { printf '  FAIL  %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }
die()  { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }
FAILURES=0

# confirm "question" -- yes only with --yes (ASSUME_YES=1) or an interactive "j".
# A Claude Code session has no terminal to answer: it has to stop, show the
# question to the operator and re-run with --yes once they agreed.
confirm() {
  if [[ "$ASSUME_YES" == 1 ]]; then
    log "Bestätigt per --yes: $1"
    return 0
  fi
  if [[ -t 0 ]]; then
    local answer
    read -r -p "$1 [j/N] " answer
    [[ "$answer" == [jJyY]* ]]
    return
  fi
  die "Bestätigung nötig: '$1' -- Betreiber fragen, dann mit --yes erneut aufrufen."
}

parse_common_args() {
  # sets ASSUME_YES; leaves the rest in REST_ARGS
  REST_ARGS=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) ASSUME_YES=1 ;;
      *) REST_ARGS+=("$1") ;;
    esac
    shift
  done
}

require_not_root() {
  [[ "$(id -u)" != 0 ]] || die "nicht als root/sudo ausführen (HOME wäre /root)."
}

load_config() {
  MERGE_ENV="${MERGE_ENV:-$HOME/merge-web-trx/merge.env}"
  if [[ -f "$MERGE_ENV" ]]; then
    # shellcheck disable=SC1090
    . "$MERGE_ENV"
  fi
  PLUTO_TX_DIR="${PLUTO_TX_DIR:-$HOME/Dokumente/plutosdr}"
  WEB_TRX_OLD_DIR="${WEB_TRX_OLD_DIR:-$HOME/Dokumente/Web-TRX}"
  MERGE_HOME="${MERGE_HOME:-$HOME/merge-web-trx}"
  WORK_DIR="${WORK_DIR:-$MERGE_HOME/work}"
  BACKUP_DIR="${BACKUP_DIR:-$MERGE_HOME/backup}"
  LOG_DIR="${LOG_DIR:-$MERGE_HOME/logs}"
  RESULTS_DIR="${RESULTS_DIR:-$MERGE_HOME/results}"
  PLUTO_TX_REMOTE_MATCH="${PLUTO_TX_REMOTE_MATCH:-jochenhammes/pluto-tx}"
  WEB_TRX_REMOTE_MATCH="${WEB_TRX_REMOTE_MATCH:-jochenhammes/Web-TRX}"
  MERGE_BRANCH="${MERGE_BRANCH:-merge-web-trx}"
  SMOKE_PORT="${SMOKE_PORT:-8322}"
  PROD_PORT="${PROD_PORT:-8321}"
  WEB_TRX_SHA="${WEB_TRX_SHA:-}"
  # shellcheck disable=SC2034
  BUILD_REPO="$WORK_DIR/pluto-tx"
  # Guard against a config that points the work area into a repository.
  case "$WORK_DIR/" in
    "$PLUTO_TX_DIR"/*|"$WEB_TRX_OLD_DIR"/*) die "WORK_DIR ($WORK_DIR) darf nicht in einem der Repos liegen." ;;
  esac
  case "$BACKUP_DIR/" in
    "$PLUTO_TX_DIR"/*|"$WEB_TRX_OLD_DIR"/*) die "BACKUP_DIR ($BACKUP_DIR) darf nicht in einem der Repos liegen." ;;
  esac
}

# start_log NAME -- everything this script prints also goes to $LOG_DIR
start_log() {
  mkdir -p "$LOG_DIR" "$RESULTS_DIR"
  LOG_FILE="$LOG_DIR/$1-$(date +%Y%m%d-%H%M%S).log"
  exec > >(tee -a "$LOG_FILE") 2>&1
  log "Log: $LOG_FILE"
}

# run_clean CMD... -- without any WEB_TRX_* from the caller's shell (a test
# must never inherit a path to the production run/ data), with the apps'
# Web-TRX integration off (the new webtrx tests switch it on themselves).
run_clean() {
  local unset_args=() var
  while IFS= read -r var; do
    unset_args+=(-u "$var")
  done < <(compgen -e | grep '^WEB_TRX_' || true)
  env "${unset_args[@]}" PLUTO_WEBTRX_CONTROL=off QT_QPA_PLATFORM=offscreen "$@"
}

repo_is_clean() {
  [[ -z "$(git -C "$1" status --porcelain)" ]]
}

# link_builds FROM TO -- the ignored native builds (rade_c, ft8_lib, ...) of
# the production checkout, symlinked into a scratch clone so tests see the
# same libraries. The symlinks go into .git/info/exclude: a symlink is a
# file to git, the "rade_c/" pattern in .gitignore would not cover it.
link_builds() {
  local from="$1" to="$2" d
  for d in rade_c ft8_lib gr-m17 gr-lora_sdr; do
    if [[ -d "$from/$d" && ! -e "$to/$d" ]]; then
      ln -s "$from/$d" "$to/$d"
      grep -qx "/$d" "$to/.git/info/exclude" 2>/dev/null || echo "/$d" >>"$to/.git/info/exclude"
    fi
  done
}

# webtrx_pid_alive DIR -- PID from DIR/run/web-trx.pid, only if that process
# is our uvicorn (same rule as the scripts); prints the PID
webtrx_pid_alive() {
  local pid
  pid="$(cat "$1/run/web-trx.pid" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null \
    && grep -qa uvicorn "/proc/$pid/cmdline" 2>/dev/null \
    && grep -qa web_trx.server "/proc/$pid/cmdline" 2>/dev/null \
    && echo "$pid"
}

established_on_port() {
  if command -v ss >/dev/null 2>&1; then
    ss -Htn state established "( sport = :$1 )" 2>/dev/null | wc -l
  else
    echo "?"
  fi
}

import_sha() {
  if [[ -n "$WEB_TRX_SHA" ]]; then
    git -C "$WEB_TRX_OLD_DIR" rev-parse --verify "$WEB_TRX_SHA^{commit}"
  else
    git -C "$WEB_TRX_OLD_DIR" rev-parse HEAD
  fi
}

summary_exit() {
  echo
  if [[ "$FAILURES" -gt 0 ]]; then
    log "$FAILURES Prüfung(en) FEHLGESCHLAGEN -- nicht weitermachen."
    exit 1
  fi
  log "Alle Prüfungen bestanden."
}
