#!/usr/bin/env bash
# Optional, separate installer for FT8 digimode support (planned "FT8" mode
# in pluto_tx TX / pluto_advanced_rx RX). Kept apart from install.sh on
# purpose, same reasoning as install-m17.sh/install-rade.sh: install.sh is
# pure apt, no source builds; kgoba/ft8_lib (https://github.com/kgoba/
# ft8_lib) isn't on apt/PyPI and needs a real build.
#
# Unlike gr-m17 (has a `make install` target) and unlike a hypothetical
# "just ctypes.CDLL() it" library, ft8_lib's own Makefile only produces a
# STATIC archive (libft8.a) plus console binaries -- there is no `.so` at
# all for Python's ctypes to load. Confirmed during this integration's own
# feasibility check that NO custom C shim source is actually needed to fix
# this: ft8_lib's real public functions (ftx_message_encode, ft8_encode,
# monitor_init/monitor_process, ftx_find_candidates, ftx_decode_candidate,
# ftx_message_decode, ...) are already plain, cleanly-named C functions, so
# simply RE-LINKING the already-built static archive (plus its fft/ KissFFT
# objects, which the Makefile does NOT bundle into libft8.a itself -- only
# links them directly into the decode_ft8 console app) into a new shared
# object exports all of them as normal dynamic symbols:
#   gcc -shared -fPIC -Wl,--whole-archive libft8.a -Wl,--no-whole-archive \
#       <fft/*.o> -lm -o libft8wrap.so
# Verified via `nm -D` that every function this project's ft8_ctypes.py
# needs is present in the resulting .so, and via a real ctypes round-trip
# (pack a message, encode tones, decode it back) that it actually works,
# not just that the symbols are present.
#
# FT8 touches BOTH pluto_tx (TX) and pluto_advanced_rx (RX). The RX side
# prefers WSJT-X's jt9 decoder when installed (this script installs the
# wsjtx apt package for it) and falls back to ft8_lib.
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-ft8.sh
#
# Safe to re-run.
set -euo pipefail

# See install-m17.sh/install-rade.sh's identical guard: run as root, the
# build in $SCRIPT_DIR/ft8_lib would end up root-owned and the apt step
# below already calls sudo itself.
if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    echo "Richtig: ./install-ft8.sh   (das Skript ruft sudo intern fuer apt-get auf)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FT8_LIB_DIR="$SCRIPT_DIR/ft8_lib"
# Pinned to the exact commit this integration's own feasibility spike
# (real ctypes pack/encode/decode round-trip, 4 realistic FT8 messages,
# all correct) was built and verified against this session -- ft8_lib has
# no versioned releases, pin rather than float.
FT8_LIB_COMMIT="9fec6ca39886edbf96f4f5e71edc76da5074e871"

echo "== ft8_lib (FT8 digimode) installer =="
echo "Repo directory: $FT8_LIB_DIR"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer only supports Debian/Ubuntu-family systems (apt-get not found)." >&2
    echo "Install cmake, make, git, and build-essential manually, then build" >&2
    echo "ft8_lib (https://github.com/kgoba/ft8_lib) yourself." >&2
    exit 1
fi

BUILD_PACKAGES=(make git build-essential)
MISSING=()
for pkg_cmd in make:make git:git gcc:build-essential; do
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
    echo "Build tools already present (make, git, gcc)."
fi

if [ ! -d "$FT8_LIB_DIR/.git" ]; then
    echo
    echo "Cloning ft8_lib..."
    git clone https://github.com/kgoba/ft8_lib.git "$FT8_LIB_DIR"
fi

echo
echo "Checking out the pinned commit $FT8_LIB_COMMIT..."
git -C "$FT8_LIB_DIR" fetch origin
git -C "$FT8_LIB_DIR" checkout -- common/monitor.c 2>/dev/null || true   # undo the log patch below
git -C "$FT8_LIB_DIR" checkout "$FT8_LIB_COMMIT"
# monitor_init() logs its FFT sizes to stderr at INFO level, once per decoded slot -- quiet it.
sed -i 's/^#define LOG_LEVEL LOG_INFO$/#define LOG_LEVEL LOG_WARN/' "$FT8_LIB_DIR/common/monitor.c"

echo
echo "Building libft8.a (position-independent, so it can be re-linked into"
echo "a shared object below)..."
cd "$FT8_LIB_DIR"
rm -rf .build libft8.a
make CFLAGS="-O3 -fPIC -DHAVE_STPCPY -I." libft8.a

echo
echo "Compiling the bundled KissFFT sources (needed by monitor_process()'s"
echo "internal FFT calls, but NOT bundled into libft8.a itself by ft8_lib's"
echo "own Makefile -- only linked directly into its decode_ft8 console app)..."
mkdir -p .build/fft
for f in fft/*.c; do
    gcc -O3 -fPIC -DHAVE_STPCPY -I. -c "$f" -o ".build/${f%.c}.o"
done

echo
echo "Re-linking libft8.a + the FFT objects into a loadable libft8wrap.so"
echo "(ft8_lib itself only produces a static archive -- see this script's"
echo "own header comment for why no hand-written C shim is needed)..."
gcc -shared -fPIC -Wl,--whole-archive libft8.a -Wl,--no-whole-archive .build/fft/*.o -lm -o libft8wrap.so
cd "$SCRIPT_DIR"

LIBDIR="$FT8_LIB_DIR"
if [ ! -f "$LIBDIR/libft8wrap.so" ]; then
    echo "WARNING: could not locate the built libft8wrap.so -- something went wrong with the build." >&2
    exit 1
fi
echo "Found libft8wrap.so in: $LIBDIR"

echo
echo "Verifying the Python side (ctypes)..."
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 - <<EOF
import ctypes
lib = ctypes.CDLL("libft8wrap.so")
for fn in ("ftx_message_encode", "ftx_message_decode", "ft8_encode", "ft4_encode",
           "monitor_init", "monitor_process", "monitor_reset", "monitor_free",
           "ftx_find_candidates", "ftx_decode_candidate"):
    getattr(lib, fn)  # raises AttributeError if missing
print("libft8wrap.so imports cleanly via ctypes, expected function surface present")
EOF

# No launcher changes needed: pluto_tx/ft8_ctypes.py and pluto_advanced_rx/ft8_ctypes.py load
# $SCRIPT_DIR/ft8_lib/libft8wrap.so directly when it isn't on LD_LIBRARY_PATH.

echo
if command -v jt9 >/dev/null 2>&1; then
    echo "WSJT-X's jt9 decoder found ($(command -v jt9)) -- the RX app uses it for FT8 (finds ~1.5x as"
    echo "many signals as ft8_lib alone)."
else
    echo "Installing WSJT-X for its jt9 decoder (the RX app's preferred FT8 decoder; ft8_lib is the"
    echo "fallback and finds ~2/3 as many signals). You may be asked for your sudo password."
    export NEEDRESTART_MODE=a
    if ! sudo apt-get install -y wsjtx; then
        echo "WARNING: could not install wsjtx -- FT8 RX will use ft8_lib only." >&2
    fi
fi

echo
echo "== Done =="
echo "FT8 is available in pluto-tx (Digimodes tab), pluto-advanced-rx (Digimodes tab) and"
echo "pluto-cli (tx ft8 / rx ... --digimode ft8)."
