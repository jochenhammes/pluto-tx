#!/usr/bin/env bash
# Optional, separate installer for RADE V1 digital voice support (planned
# "RADE" mode in pluto_tx TX / pluto_advanced_rx RX). Kept apart from
# install.sh on purpose, same reasoning as install-m17.sh: install.sh is
# pure apt, no source builds; freedv/rade_c (https://github.com/freedv/
# rade_c) isn't on apt/PyPI and needs a real cmake/C build, including its
# own bundled, patched Opus/FARGAN fork (fetched automatically by CMake).
#
# Unlike gr-m17, rade_c's CMakeLists.txt has no `make install` target --
# there is nothing to install into a prefix. This script keeps the build
# tree in place at $SCRIPT_DIR/rade_c and points LD_LIBRARY_PATH/PATH at
# its build/src directory directly. Two artifacts matter to this project,
# both in that one directory:
#   - librade.so   (ctypes-loaded by rade_ctypes.py -- the RADE modem)
#   - lpcnet_demo  (run as a subprocess by lpcnet_subprocess.py -- the
#                   FARGAN/LPCNet speech<->feature-vector vocoder, NOT
#                   exported by librade.so itself, confirmed via nm -D
#                   during this integration's feasibility spike)
# RADE touches BOTH pluto_tx (TX) and pluto_advanced_rx (RX) -- unlike M17,
# which only needs pluto-tx -- so this script updates both launchers.
#
# Launcher regeneration is MERGE-SAFE: if install-m17.sh (or a previous run
# of this script) already added its own LD_LIBRARY_PATH entry to a
# launcher, that entry is preserved, not clobbered -- read the launcher's
# current export line (if any) and fold this script's own directory into
# it, rather than overwriting wholesale the way install-m17.sh's own
# generator does (a pre-existing limitation there: re-running install-m17.sh
# after this script would drop RADE's PATH addition -- not touched here,
# out of scope for this installer, but worth knowing if that order comes up).
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-rade.sh
#
# Safe to re-run.
set -euo pipefail

# See install-m17.sh's identical guard for the full reasoning: this script
# builds into $SCRIPT_DIR/rade_c and regenerates launchers under
# $HOME/.local/bin -- running it with sudo/as root sets $HOME=/root and
# silently sends the regenerated launchers to the wrong place.
if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    echo "Richtig: ./install-rade.sh   (das Skript ruft sudo intern fuer apt-get auf)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RADE_C_DIR="$SCRIPT_DIR/rade_c"
# Pinned to the exact commit this integration's offline feasibility
# round-trip (WAV -> lpcnet_demo -> radae_tx -> IQ -> radae_rx ->
# lpcnet_demo -> WAV) was built and verified against this session --
# understandable decoded speech, real-time factor ~0.03. rade_c's README
# only documents building against its own HEAD; pin rather than float.
RADE_C_COMMIT="0d5c5f7c27e650e3ca8d9e0f7d4781f2e73ee2b0"

echo "== rade_c (RADE V1 digital voice) installer =="
echo "Repo directory: $RADE_C_DIR"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer only supports Debian/Ubuntu-family systems (apt-get not found)." >&2
    echo "Install cmake, make, git, build-essential, autoconf, automake, libtool, and wget" >&2
    echo "manually, then build rade_c (https://github.com/freedv/rade_c) yourself." >&2
    exit 1
fi

# autoconf/automake/libtool are needed by the bundled Opus fork's own
# autogen.sh step (part of CMake's ExternalProject_Add for Opus/FARGAN).
# wget is needed by a SECOND, separate download that autogen.sh triggers,
# not just obvious from the top-level "fetch Opus" step: it runs
# dnn/download_model.sh, which fetches a FARGAN/LPCNet model archive from
# media.xiph.org (a completely different host than the GitHub Opus zip
# above) via wget, falling back to curl only if wget is missing. Neither is
# guaranteed present on a minimal system (found and fixed this session --
# the CMake-driven Opus zip download itself needs neither, since that uses
# cmake's own bundled libcurl, which is why this gap wasn't obvious from
# the top-level build failing early).
BUILD_PACKAGES=(cmake make git build-essential autoconf automake libtool wget)
MISSING=()
for pkg_cmd in cmake:cmake make:make git:git autoconf:autoconf automake:automake libtoolize:libtool wget:wget; do
    cmd="${pkg_cmd%%:*}"
    command -v "$cmd" >/dev/null 2>&1 || MISSING+=("${pkg_cmd##*:}")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing missing build tools: ${MISSING[*]}"
    echo "(you may be asked for your sudo password)"
    export NEEDRESTART_MODE=a
    sudo apt-get update
    sudo apt-get install -y "${BUILD_PACKAGES[@]}"
else
    echo "Build tools already present (cmake, make, git, autoconf, automake, libtool, wget)."
fi

if [ ! -d "$RADE_C_DIR/.git" ]; then
    echo
    echo "Cloning rade_c..."
    git clone https://github.com/freedv/rade_c.git "$RADE_C_DIR"
fi

echo
echo "Checking out the pinned commit $RADE_C_COMMIT..."
git -C "$RADE_C_DIR" fetch origin
git -C "$RADE_C_DIR" checkout "$RADE_C_COMMIT"

echo
echo "Configuring and building rade_c (this also fetches and builds a"
echo "patched Opus/FARGAN fork -- can take a few minutes)..."
mkdir -p "$RADE_C_DIR/build"

# wget wrapper with a real timeout: cmake's ExternalProject build of the
# patched Opus fork runs its own autogen.sh, which in turn calls
# dnn/download_model.sh to fetch a ~100MB FARGAN/LPCNet model from
# media.xiph.org via plain `wget` with NO timeout flag of its own. If that
# host is unreachable (server down, routing issue), the whole build just
# hangs indefinitely with no error -- found on a real installation.
# download_model.sh lives inside the Opus fork's own source tree (only
# fetched/unpacked by cmake at build time), so it can't be patched directly
# from here -- instead, put a wrapper earlier on PATH that forces a real
# timeout on every `wget` call for the duration of this build, including
# calls made deep inside the ExternalProject build (PATH is searched before
# the real /usr/bin/wget regardless of who invokes it).
WGET_WRAP_DIR="$(mktemp -d)"
trap 'rm -rf "$WGET_WRAP_DIR"' EXIT
cat > "$WGET_WRAP_DIR/wget" <<'WGET_EOF'
#!/bin/sh
exec /usr/bin/wget --timeout=30 --tries=2 "$@"
WGET_EOF
chmod +x "$WGET_WRAP_DIR/wget"
export PATH="$WGET_WRAP_DIR:$PATH"

cd "$RADE_C_DIR/build"
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j"$(nproc)"
cd "$SCRIPT_DIR"

LIBDIR="$(find "$RADE_C_DIR/build" -iname "librade.so" -printf '%h\n' 2>/dev/null | head -1)"
if [ -z "$LIBDIR" ]; then
    echo "WARNING: could not locate the built librade.so -- something went wrong with the build." >&2
    exit 1
fi
LPCNET_DEMO="$LIBDIR/lpcnet_demo"
if [ ! -x "$LPCNET_DEMO" ]; then
    echo "WARNING: could not locate the built lpcnet_demo at $LPCNET_DEMO." >&2
    exit 1
fi
echo "Found librade.so and lpcnet_demo in: $LIBDIR"

echo
echo "Verifying the Python side (ctypes)..."
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 - <<EOF
import ctypes
lib = ctypes.CDLL("librade.so")
for fn in ("rade_open", "rade_tx", "rade_rx", "rade_close"):
    getattr(lib, fn)  # raises AttributeError if missing
print("librade.so imports cleanly via ctypes, rade_api.h surface present")
EOF

echo "Verifying lpcnet_demo runs..."
"$LPCNET_DEMO" >/dev/null 2>&1 || true  # no-args run just prints usage, exit code not meaningful here
echo "lpcnet_demo present and executable."

# Fold this script's LIBDIR/PATH additions into an existing launcher's
# current export lines instead of overwriting them -- see the module
# docstring above for why (M17's LD_LIBRARY_PATH entry must survive).
# Membership-checked against every ':'-separated component, not just
# whole-string equality -- a plain equality check only catches the
# single-entry case; on a launcher that already has multiple entries (e.g.
# pluto-tx after gr-m17), it would keep re-prepending LIBDIR on every rerun.
prepend_if_missing() {
    local dir="$1" list="$2"
    if [ -z "$list" ]; then
        echo "$dir"
        return
    fi
    local IFS=':'
    local part
    for part in $list; do
        if [ "$part" = "$dir" ]; then
            echo "$list"
            return
        fi
    done
    echo "$dir:$list"
}

regenerate_launcher() {
    local name="$1" module_invocation="$2"
    local launcher="$HOME/.local/bin/$name"
    local existing_ld="" existing_path=""
    if [ -f "$launcher" ]; then
        existing_ld="$(grep -oP '(?<=export LD_LIBRARY_PATH=")[^"]*(?=:\$\{LD_LIBRARY_PATH:-\}")' "$launcher" 2>/dev/null || true)"
        existing_path="$(grep -oP '(?<=export PATH=")[^"]*(?=:\$\{PATH:-\}")' "$launcher" 2>/dev/null || true)"
    fi
    local new_ld new_path
    new_ld="$(prepend_if_missing "$LIBDIR" "$existing_ld")"
    new_path="$(prepend_if_missing "$LIBDIR" "$existing_path")"

    mkdir -p "$HOME/.local/bin"
    cat > "$launcher" <<EOF
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$new_ld:\${LD_LIBRARY_PATH:-}"
export PATH="$new_path:\${PATH:-}"
cd "$SCRIPT_DIR" && exec python3 -m $module_invocation "\$@"
EOF
    chmod +x "$launcher"
    echo "Updated $launcher"
}

echo
echo "Updating the pluto-tx, pluto-advanced-rx and pluto-cli launchers..."
regenerate_launcher "pluto-tx" "pluto_tx.app --gui"
regenerate_launcher "pluto-advanced-rx" "pluto_advanced_rx.app"
regenerate_launcher "pluto-cli" "pluto_cli.app"

echo
echo "== Done =="
echo "RADE V1 support will be wired into pluto_tx/pluto_advanced_rx in a"
echo "later step of this integration -- this script only builds and locates"
echo "the artifacts (librade.so, lpcnet_demo) and updates the launchers so"
echo "they'll be found once that code lands."
