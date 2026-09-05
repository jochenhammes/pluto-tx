#!/usr/bin/env bash
# Optional, separate installer for M17 digital voice support (pluto_tx's
# "M17" mode). Kept apart from install.sh on purpose: install.sh is pure
# apt, no source builds, by design -- gr-m17 (https://github.com/M17-Project/
# gr-m17) isn't on apt/PyPI and needs a real cmake/C++ build. M17 is fully
# optional: pluto_tx works fine for FM/SSB without ever running this script
# (its "M17" mode entry just shows greyed-out with an explanatory tooltip).
#
# Builds gr-m17 to a LOCAL prefix ($HOME/.local) -- no sudo needed for the
# actual gr-m17 build/install, only for the handful of apt build-tool
# packages below. $HOME/.local/lib/python3.*/site-packages is already on
# Python's default sys.path, so no PYTHONPATH changes are needed; the
# native shared library needs LD_LIBRARY_PATH, which this script wires
# straight into the pluto-tx launcher script install.sh already created
# (regenerating it), rather than asking you to edit shell rc files.
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-m17.sh
#
# Safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GR_M17_DIR="$SCRIPT_DIR/gr-m17"
INSTALL_PREFIX="$HOME/.local"
# Pinned to the exact commit this integration was built and verified
# against this session (gr-m17's README only claims testing against GNU
# Radio 3.10.9.2/3.10.10.0, not our 3.10.12 -- it DID build and pass a real
# offline coder->decoder round-trip plus a real low-power PTT test here,
# but "main" may have moved since; pin rather than float).
GR_M17_COMMIT="36267b114b41920b3b62d9545afe7d7c854801bf"

echo "== gr-m17 (M17 digital voice) installer =="
echo "Repo directory: $SCRIPT_DIR"
echo "Install prefix: $INSTALL_PREFIX (no sudo needed for the gr-m17 build itself)"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer only supports Debian/Ubuntu-family systems (apt-get not found)." >&2
    echo "Install cmake, make, doxygen, git, g++, and gnuradio-dev manually, then build" >&2
    echo "gr-m17 (https://github.com/M17-Project/gr-m17) yourself with CMAKE_INSTALL_PREFIX" >&2
    echo "pointed at a directory on your PYTHONPATH/LD_LIBRARY_PATH." >&2
    exit 1
fi

BUILD_PACKAGES=(cmake make doxygen git build-essential)
MISSING=()
for pkg_cmd in cmake:cmake make:make doxygen:doxygen git:git; do
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
    echo "Build tools already present (cmake, make, doxygen, git)."
fi

if [ ! -d "$GR_M17_DIR/.git" ]; then
    echo
    echo "Cloning gr-m17 (with its libm17/codec2-mod/micro-ecc/tinier-aes submodules)..."
    git clone --recursive https://github.com/M17-Project/gr-m17.git "$GR_M17_DIR"
fi

echo
echo "Checking out the pinned commit $GR_M17_COMMIT..."
git -C "$GR_M17_DIR" fetch origin
git -C "$GR_M17_DIR" checkout "$GR_M17_COMMIT"
git -C "$GR_M17_DIR" submodule update --init --recursive

echo
echo "Configuring and building gr-m17 (install prefix: $INSTALL_PREFIX)..."
mkdir -p "$GR_M17_DIR/build"
cd "$GR_M17_DIR/build"
cmake .. -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
make install
cd "$SCRIPT_DIR"

# Locate the installed shared library's directory -- this is what needs to
# be on LD_LIBRARY_PATH for "from gnuradio import m17" to actually import
# (the Python package itself lands in $INSTALL_PREFIX/lib/python3.*/
# site-packages, already on sys.path by default; only the native .so needs
# this extra step).
LIBDIR="$(find "$INSTALL_PREFIX/lib" -iname "libgnuradio-m17.so*" -printf '%h\n' 2>/dev/null | head -1)"
if [ -z "$LIBDIR" ]; then
    echo "WARNING: could not locate the installed libgnuradio-m17.so -- something went wrong with the build/install." >&2
    exit 1
fi
echo "Found libgnuradio-m17.so in: $LIBDIR"

echo
echo "Verifying the Python side..."
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 - <<EOF
import sys
try:
    from gnuradio import m17  # noqa: F401
except ImportError as e:
    print(f"FAILED: {e}", file=sys.stderr)
    sys.exit(1)
print("gnuradio.m17 imports cleanly:", m17.m17_coder, m17.m17_decoder, m17.codec2_encoder, m17.codec2_decoder)
EOF

echo
echo "Updating the pluto-tx launcher (~/.local/bin/pluto-tx) to set LD_LIBRARY_PATH..."
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/pluto-tx" <<EOF
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$LIBDIR:\${LD_LIBRARY_PATH:-}"
cd "$SCRIPT_DIR" && exec python3 -m pluto_tx.app --gui "\$@"
EOF
chmod +x "$HOME/.local/bin/pluto-tx"
echo "Done -- pluto-tx now finds gr-m17 automatically."

echo
echo "== Done =="
echo "Start pluto_tx as usual (pluto-tx, or python3 -m pluto_tx.app --gui) --"
echo "the M17 mode entry should now be selectable instead of greyed out."
echo
echo "If you run pluto_tx some OTHER way (not the pluto-tx launcher), set this"
echo "in your shell first:"
echo "    export LD_LIBRARY_PATH=\"$LIBDIR:\$LD_LIBRARY_PATH\""
