#!/usr/bin/env bash
# Optional, separate installer for LoRa CSS PHY support (the Meshtastic
# digimode of pluto_tx / pluto_advanced_rx). Kept apart from install.sh on
# purpose: install.sh is pure apt, no source builds, by design -- gr-lora_sdr (https://github.com/tapparelj/
# gr-lora_sdr) isn't on apt/PyPI and needs a real cmake/C++ build. LoRa is
# fully optional: pluto_tx/pluto_advanced_rx work fine without ever running
# this script (the Meshtastic entry just shows greyed-out with an
# explanatory tooltip).
#
# Builds gr-lora_sdr to a LOCAL prefix ($HOME/.local) -- no sudo needed for
# the actual gr-lora_sdr build/install, only for the handful of apt
# build-tool packages below. Same exact structure as install-m17.sh
# (regenerate_launcher()/prepend_if_missing(), merge-safe LD_LIBRARY_PATH
# handling) -- deliberately copied rather than reinvented, see that
# script's own comments for the real bugs this pattern already fixed once
# (root/sudo silently redirecting $HOME, launcher regeneration silently
# dropping another install script's PATH/LD_LIBRARY_PATH entry).
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-lora.sh
#
# Safe to re-run.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    echo "Richtig: ./install-lora.sh   (das Skript ruft sudo intern fuer apt-get auf)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GR_LORA_SDR_DIR="$SCRIPT_DIR/gr-lora_sdr"
INSTALL_PREFIX="$HOME/.local"
# Pinned to master's HEAD as of 2026-09-18 (verified: gr-lora_sdr's own
# README targets GNU Radio 3.10 specifically on this branch, unlike its
# own explicitly-named unmaintained/gnuradio-3.7_v0.1 and
# unmaintained/gnuradio-3.8_v0.3 branches -- so master is the right
# branch, but still pinned rather than floated, same reasoning as
# install-m17.sh's GR_M17_COMMIT: verify once against this exact commit,
# don't silently rebuild against whatever master has moved to later).
GR_LORA_SDR_COMMIT="862746dd1cf635c9c8a4bfbaa2c3a0ec3a5306c9"

echo "== gr-lora_sdr (LoRa CSS PHY) installer =="
echo "Repo directory: $SCRIPT_DIR"
echo "Install prefix: $INSTALL_PREFIX (no sudo needed for the gr-lora_sdr build itself)"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer only supports Debian/Ubuntu-family systems (apt-get not found)." >&2
    echo "Install cmake, make, git, g++, gnuradio-dev, libvolk-dev, libboost-all-dev," >&2
    echo "libuhd-dev, and pybind11-dev manually, then build gr-lora_sdr" >&2
    echo "(https://github.com/tapparelj/gr-lora_sdr) yourself with CMAKE_INSTALL_PREFIX" >&2
    echo "pointed at a directory on your PYTHONPATH/LD_LIBRARY_PATH." >&2
    exit 1
fi

# gr-lora_sdr's own README (fetched and verified this session) lists:
# GNU Radio 3.10, cmake, libvolk, boost, UHD, gcc>9.3, pybind11.
# gnuradio-dev is REQUIRED (not just the "gnuradio" runtime package
# install.sh already installs) for the same find_package(Gnuradio "3.10"
# REQUIRED) reason already documented in install-m17.sh -- and it's only
# an apt "Recommends" of "gnuradio", not a hard "Depends", so it needs its
# own explicit check on a system with APT::Install-Recommends disabled.
BUILD_PACKAGES=(cmake make git build-essential gnuradio-dev libvolk-dev libboost-all-dev libuhd-dev pybind11-dev)
MISSING=()
for pkg_cmd in cmake:cmake make:make git:git; do
    cmd="${pkg_cmd%%:*}"
    command -v "$cmd" >/dev/null 2>&1 || MISSING+=("${pkg_cmd##*:}")
done
# The rest have no standalone CLI command to probe for -- check dpkg's own
# record instead so a re-run doesn't reinstall them every time.
for dev_pkg in gnuradio-dev libvolk-dev libboost-all-dev libuhd-dev pybind11-dev; do
    dpkg -s "$dev_pkg" >/dev/null 2>&1 || MISSING+=("$dev_pkg")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing missing build tools: ${MISSING[*]}"
    echo "(you may be asked for your sudo password)"
    export NEEDRESTART_MODE=a
    sudo apt-get update
    sudo apt-get install -y "${BUILD_PACKAGES[@]}"
else
    echo "Build tools already present (cmake, make, git, gnuradio-dev, libvolk-dev, libboost-all-dev, libuhd-dev, pybind11-dev)."
fi

if [ ! -d "$GR_LORA_SDR_DIR/.git" ]; then
    echo
    echo "Cloning gr-lora_sdr..."
    git clone https://github.com/tapparelj/gr-lora_sdr.git "$GR_LORA_SDR_DIR"
fi

echo
echo "Checking out the pinned commit $GR_LORA_SDR_COMMIT..."
git -C "$GR_LORA_SDR_DIR" fetch origin
git -C "$GR_LORA_SDR_DIR" checkout "$GR_LORA_SDR_COMMIT"

echo
echo "Configuring and building gr-lora_sdr (install prefix: $INSTALL_PREFIX)..."
mkdir -p "$GR_LORA_SDR_DIR/build"
cd "$GR_LORA_SDR_DIR/build"
cmake .. -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
make install
cd "$SCRIPT_DIR"

# Locate the installed shared library's directory -- same reasoning as
# install-m17.sh: $INSTALL_PREFIX/lib/python3.*/site-packages is already
# on Python's default sys.path, only the native .so needs LD_LIBRARY_PATH.
LIBDIR="$(find "$INSTALL_PREFIX/lib" -iname "libgnuradio-lora_sdr.so*" -printf '%h\n' 2>/dev/null | head -1)"
if [ -z "$LIBDIR" ]; then
    echo "WARNING: could not locate the installed libgnuradio-lora_sdr.so -- something went wrong with the build/install." >&2
    exit 1
fi
echo "Found libgnuradio-lora_sdr.so in: $LIBDIR"

echo
echo "Verifying the Python side..."
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 - <<EOF
import sys
try:
    from gnuradio import lora_sdr  # noqa: F401
except ImportError as e:
    print(f"FAILED: {e}", file=sys.stderr)
    sys.exit(1)
print("gnuradio.lora_sdr imports cleanly:", lora_sdr.lora_sdr_lora_tx, lora_sdr.lora_sdr_lora_rx)
EOF

echo
echo "Updating the pluto-tx/pluto-advanced-rx/pluto-cli launchers to set LD_LIBRARY_PATH..."
# Merge-safe regeneration -- identical function to install-m17.sh's own
# (copied verbatim, not reinvented): reads whatever LD_LIBRARY_PATH/PATH
# the launcher already exports (e.g. from a prior install-m17.sh/
# install-rade.sh run) and folds this script's own LIBDIR in, preserving
# everything else untouched.
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
    local new_ld
    new_ld="$(prepend_if_missing "$LIBDIR" "$existing_ld")"

    mkdir -p "$HOME/.local/bin"
    {
        echo '#!/usr/bin/env bash'
        echo "export LD_LIBRARY_PATH=\"$new_ld:\${LD_LIBRARY_PATH:-}\""
        if [ -n "$existing_path" ]; then
            echo "export PATH=\"$existing_path:\${PATH:-}\""
        fi
        echo "cd \"$SCRIPT_DIR\" && exec python3 -m $module_invocation \"\$@\""
    } > "$launcher"
    chmod +x "$launcher"
    echo "Updated $launcher"
}

regenerate_launcher "pluto-tx" "pluto_tx.app --gui"
regenerate_launcher "pluto-advanced-rx" "pluto_advanced_rx.app"
regenerate_launcher "pluto-cli" "pluto_cli.app"
echo "Done -- pluto-tx, pluto-advanced-rx and pluto-cli now find gr-lora_sdr automatically."

echo
echo "== Done =="
echo "The Meshtastic (LoRa) entry in both apps' Digimode combo is now selectable"
echo "-- as long as the Python packages 'meshtastic' and 'cryptography' are"
echo "also installed (the packet layer, pure Python, from PyPI):"
if python3 -c "import meshtastic, cryptography" 2>/dev/null; then
    echo "    found -- nothing more to do."
else
    echo "    NOT found. Install them with:"
    echo "        pip install --user meshtastic cryptography"
    echo "    (on a PEP 668 system, i.e. 'externally-managed-environment' error, add"
    echo "     --break-system-packages: it still only writes to ~/.local, but is your call)"
fi
echo
echo "If you run either app some OTHER way (not via these launchers), set"
echo "this in your shell first:"
echo "    export LD_LIBRARY_PATH=\"$LIBDIR:\$LD_LIBRARY_PATH\""
