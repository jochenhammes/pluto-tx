#!/usr/bin/env bash
# M3 -- builds the merge in a SCRATCH CLONE ($WORK_DIR/pluto-tx, branch
# $MERGE_BRANCH). The production checkout is only read (cloned from).
# Eight commits, each followed by `40_verify.sh --quick`:
#   1 import Web-TRX as web-trx/ (git archive of the chosen commit, one commit)
#   2 CI workflow
#   3 backend: paths, /health devices, access-log filter (+ tests)
#   4 scripts: start/stop/status.sh, install-web-trx.sh
#   5 pluto_tx/webtrx_control.py (+ tests)
#   6 GUI: pluto_tx/webtrx_widget.py, rows + guards in both apps (+ tests)
#   7 documentation
#   8 comments, remove "merge web-trx/"
#
#   30_build_merge.sh           build (refuses if the scratch clone exists)
#   30_build_merge.sh --fresh   delete the scratch clone and build again
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
parse_common_args "$@"
require_not_root
load_config
start_log 30_build_merge

FRESH=0
for arg in "${REST_ARGS[@]}"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    *) die "unbekannte Option $arg" ;;
  esac
done

case "$MERGE_DIR/" in
  "$WORK_DIR"/*) die "30_build_merge.sh aus dem Produktiv-Checkout aufrufen, nicht aus $WORK_DIR (Commit 8 löscht diesen Ordner)" ;;
esac

if [[ -e "$BUILD_REPO" ]]; then
  [[ "$FRESH" == 1 ]] || die "$BUILD_REPO existiert schon -- mit --fresh neu bauen"
  case "$BUILD_REPO" in "$WORK_DIR"/*) ;; *) die "unerwarteter Pfad $BUILD_REPO" ;; esac
  rm -rf "$BUILD_REPO"
fi
mkdir -p "$WORK_DIR"

SHA="$(import_sha)"
SHORT="$(git -C "$WEB_TRX_OLD_DIR" rev-parse --short "$SHA")"
log "Web-TRX-Stand für den Import: $SHA"

log "Scratch-Klon $BUILD_REPO"
git clone -q --no-hardlinks "$PLUTO_TX_DIR" "$BUILD_REPO"
cd "$BUILD_REPO"
prod_origin_main="$(git -C "$PLUTO_TX_DIR" rev-parse origin/main)"
[[ "$(git rev-parse HEAD)" == "$prod_origin_main" ]] || die "Klon-HEAD != origin/main der Produktion -- 00_preflight.sh erneut"
git remote add github "$(git -C "$PLUTO_TX_DIR" remote get-url origin)"
git checkout -q -b "$MERGE_BRANCH"
link_builds "$PLUTO_TX_DIR" "$BUILD_REPO"
# Web-TRX objects, so the patches can fall back to a 3-way merge if Web-TRX
# moved on since they were drafted (never pushed: not reachable from the branch)
git fetch -q "$WEB_TRX_OLD_DIR" "+refs/heads/main:refs/webtrx-import/main"
git merge-base --is-ancestor "$SHA" refs/webtrx-import/main 2>/dev/null \
  || git fetch -q "$WEB_TRX_OLD_DIR" "+$SHA:refs/webtrx-import/sha" \
  || die "Commit $SHA nicht aus $WEB_TRX_OLD_DIR holbar"
echo "$SHA" >"$WORK_DIR/import-sha"
git rev-parse HEAD >"$WORK_DIR/base-sha"

apply_patch() {
  local patch="$PATCH_DIR/$1"
  [[ -f "$patch" ]] || die "Patch $patch fehlt"
  if git apply --index --whitespace=nowarn "$patch"; then
    ok "Patch $1"
  elif git apply --index --3way --whitespace=nowarn "$patch"; then
    warn "Patch $1 nur per 3-Wege-Merge anwendbar -- Ergebnis prüfen: git show HEAD nach dem Commit"
    if git diff --name-only --diff-filter=U | grep -q .; then
      die "Konflikte in: $(git diff --name-only --diff-filter=U | tr '\n' ' ') -- siehe patches/README.md (von Hand einarbeiten)"
    fi
  else
    die "Patch $1 lässt sich nicht anwenden -- siehe patches/README.md"
  fi
}

copy_file() {  # copy_file RELPATH [+x]
  mkdir -p "$(dirname "$1")"
  cp "$FILES_DIR/$1" "$1"
  if [[ "${2:-}" == "+x" ]]; then
    chmod 755 "$1"
    git add --chmod=+x "$1"
  else
    chmod 644 "$1"
    git add "$1"
  fi
}

commit() {
  git commit -q -F -
  log "Commit $(git log -1 --format='%h %s')"
  "$MERGE_SCRIPTS_DIR/40_verify.sh" --quick "$BUILD_REPO"
}

# --- 1: import -------------------------------------------------------------
log "Commit 1: Import"
mkdir web-trx
git -C "$WEB_TRX_OLD_DIR" archive "$SHA" | tar -x -C web-trx
# the submodule becomes an empty directory in the archive; rmdir fails loudly otherwise
rmdir web-trx/vendor/pluto-tx web-trx/vendor
cmp -s web-trx/LICENSE LICENSE || die "web-trx/LICENSE unterscheidet sich von LICENSE -- Betreiber fragen"
rm web-trx/LICENSE web-trx/.gitmodules
rm -r web-trx/.github
git add web-trx
commit <<EOF
web-trx: import Web-TRX $SHORT as web-trx/

Web-TRX (browser UI for pluto-tx/pluto-advanced-rx) becomes part of this
repository, next to pluto-tx, pluto-advanced-rx and pluto-cli.

Imported tree: jochenhammes/Web-TRX @ $SHA
(the individual history stays in that repository, which gets archived).

Left out on purpose: vendor/pluto-tx (the submodule -- web-trx now uses
this checkout), .gitmodules, .github/ (replaced by
.github/workflows/web-trx.yml) and LICENSE (identical to ./LICENSE).
EOF

# --- 2: CI -----------------------------------------------------------------
log "Commit 2: CI"
copy_file .github/workflows/web-trx.yml
commit <<'EOF'
CI: web-trx backend/frontend and the apps' Web-TRX control

First CI workflow in this repository. Runs on every push and PR (no paths
filter: web-trx imports pluto_tx/pluto_advanced_rx). GNU Radio is not
available on the runners; tests that need it skip themselves.
EOF

# --- 3: backend ------------------------------------------------------------
log "Commit 3: Backend"
copy_file web-trx/backend/web_trx/pluto_path.py
apply_patch 03-web-trx-backend.patch
commit <<'EOF'
web-trx backend: use this checkout, report held devices on /health

- pluto_path: pluto-tx is the repository root (parents[3]) unless
  WEB_TRX_PLUTO_TX_PATH says otherwise; test_modes checks against it too.
- /health tells loopback clients which SDRs the server holds (a direction
  still being re-opened after a restart counts as held), for the TX/RX
  apps' device-conflict guards.
- GET /health stays out of the access log (the apps poll it).
EOF

# --- 4: scripts ------------------------------------------------------------
log "Commit 4: Skripte"
copy_file web-trx/scripts/start.sh +x
copy_file web-trx/scripts/stop.sh +x
copy_file web-trx/scripts/status.sh +x
copy_file install-web-trx.sh +x
commit <<'EOF'
web-trx scripts: start/stop lock, PID check, status.sh; install-web-trx.sh

- start.sh/stop.sh share a flock (run/start.lock), trust a PID file only if
  /proc/<pid>/cmdline is our uvicorn, and return distinct exit codes
  (start: 0/3/4/1, stop: 0/2/1). Default pluto-tx path: this checkout.
- status.sh: python3 -m pluto_tx.webtrx_control status.
- install-web-trx.sh: venv with system site packages, backend, frontend
  build, self-test, ~/.local/bin/web-trx. install.sh stays unchanged.
EOF

# --- 5: control ------------------------------------------------------------
log "Commit 5: Steuerung"
copy_file pluto_tx/webtrx_control.py
copy_file tests/test_webtrx_control.py
commit <<'EOF'
pluto_tx.webtrx_control: start, stop and query Web-TRX (Qt-free)

Reads web-trx.env like start.sh, checks the PID against /proc, asks
/health (no proxy, self-signed TLS), runs start.sh/stop.sh in their own
session. CLI: python3 -m pluto_tx.webtrx_control status|url|devices.
Tests run the real scripts against a stand-in server in a temp directory.
EOF

# --- 6: GUI ----------------------------------------------------------------
log "Commit 6: GUI"
copy_file pluto_tx/webtrx_widget.py
copy_file tests/test_webtrx_widget.py
copy_file tests/test_webtrx_gui_integration.py
apply_patch 06-gui-integration.patch
commit <<'EOF'
TX/RX apps: Web-TRX row with Start/Stop/Open and device guards

A row in both apps shows the Web-TRX server state. Start asks first if the
app has a device open; Connect warns if Web-TRX holds that device type; the
automatic connect at app start is skipped then. The server keeps running
when the app closes. Insertions only; a failing row never stops the app;
polling starts at the first showEvent; PLUTO_WEBTRX_CONTROL=off disables it.
EOF

# --- 7: docs ---------------------------------------------------------------
log "Commit 7: Doku"
apply_patch 07-docs.patch
commit <<'EOF'
docs: four programs in one repository; web-trx install and operation

README: web-trx next to pluto-tx, pluto-advanced-rx and pluto-cli,
install-web-trx.sh, the Web-TRX row in the apps, structure.
web-trx/README.md and docs/: installation inside pluto-tx, launcher,
exit codes, no submodule; FT8 plan rules: whole repository editable.
EOF

# --- 8: comments + remove drafts --------------------------------------------
log "Commit 8: Kommentare, Entwurfsordner entfernen"
apply_patch 08-comments.patch
git rm -r -q "merge web-trx"
commit <<'EOF'
web-trx: comments point to pluto-tx in this repo; drop merge drafts

Code comments no longer mention vendor/pluto-tx. Removes the temporary
"merge web-trx/" folder that held the drafts for this merge.
EOF

echo
git log --oneline "$(cat "$WORK_DIR/base-sha")..HEAD"
log "Merge gebaut in $BUILD_REPO (Branch $MERGE_BRANCH). Weiter mit 40_verify.sh (voll)."
