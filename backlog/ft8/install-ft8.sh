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
# FT8 touches BOTH pluto_tx (TX) and pluto_advanced_rx (RX) -- like RADE,
# unlike M17 -- so this script updates both launchers, merge-safely (see
# install-rade.sh's own identical comment for the full reasoning: a
# pre-existing LD_LIBRARY_PATH entry from M17/RADE's own installers must
# survive, not get clobbered).
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-ft8.sh
#
# Safe to re-run.
set -euo pipefail

# See install-m17.sh/install-rade.sh's identical guard for the full
# reasoning: this script builds into $SCRIPT_DIR/ft8_lib and regenerates
# launchers under $HOME/.local/bin -- running it with sudo/as root sets
# $HOME=/root and silently sends the regenerated launchers to the wrong
# place.
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
git -C "$FT8_LIB_DIR" checkout "$FT8_LIB_COMMIT"

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

# Fold this script's LIBDIR addition into an existing launcher's current
# export lines instead of overwriting them -- see install-rade.sh's own
# identical comment for why (M17/RADE's own LD_LIBRARY_PATH entries must
# survive). Membership-checked against every ':'-separated component, not
# just whole-string equality -- a plain equality check only catches the
# single-entry case; on a launcher that already has multiple entries, it
# would keep re-prepending LIBDIR on every rerun.
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
        # FT8 itself has no companion executable to add to PATH (unlike
        # RADE's lpcnet_demo) -- but a PRE-EXISTING PATH export line (from
        # install-rade.sh's own launcher regeneration) must still be
        # preserved here, or rewriting the launcher below would silently
        # drop it.
        existing_path="$(grep -oP '(?<=export PATH=")[^"]*(?=:\$\{PATH:-\}")' "$launcher" 2>/dev/null || true)"
    fi
    local new_ld
    new_ld="$(prepend_if_missing "$LIBDIR" "$existing_ld")"

    mkdir -p "$HOME/.local/bin"
    {
        echo "#!/usr/bin/env bash"
        echo "export LD_LIBRARY_PATH=\"$new_ld:\${LD_LIBRARY_PATH:-}\""
        if [ -n "$existing_path" ]; then
            echo "export PATH=\"$existing_path:\${PATH:-}\""
        fi
        echo "cd \"$SCRIPT_DIR\" && exec python3 -m $module_invocation \"\$@\""
    } > "$launcher"
    chmod +x "$launcher"
    echo "Updated $launcher"
}

echo
echo "Updating the pluto-tx and pluto-advanced-rx launchers..."
regenerate_launcher "pluto-tx" "pluto_tx.app --gui"
regenerate_launcher "pluto-advanced-rx" "pluto_advanced_rx.app"

echo
echo "== Done =="
echo "FT8 support will be wired into pluto_tx/pluto_advanced_rx in a later"
echo "step of this integration -- this script only builds libft8wrap.so and"
echo "updates the launchers so it'll be found once that code lands."
