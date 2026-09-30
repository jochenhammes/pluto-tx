#!/usr/bin/env bash
# Optional, separate installer for JS8 (JS8Call) support in pluto-tx, pluto-advanced-rx, pluto-cli and
# Web-TRX (docs/JS8_PLAN.md). Same reasoning as install-ft8.sh: install.sh stays pure apt.
#
# What it does:
#   1. clones JS8Call-improved at the pinned tag (docs/js8/SPEC.md) into ./js8call (gitignored)
#   2. extracts the JSC word lists (2 x 262144 entries, needed for JS8 free text) into
#      js8call/jsc.json and checks that the checked-in pluto_tx/js8_tables.py still matches the source
#   3. builds the reference decoder tools/js8ref (links the unmodified JS8Call sources; the RX side's
#      preferred JS8 decoder) into js8call/build-js8ref/js8ref -- needs qt6-base-dev, libfftw3-dev,
#      libboost-dev (installed via apt if missing)
#   4. self-test: the Python codec reproduces JS8Call's tones for a reference frame, and pluto_tx /
#      pluto_advanced_rx import with JS8 available
#   5. optional (--with-js8call): the distribution's JS8Call program (apt package js8call), e.g. to check
#      your transmissions with the real JS8Call GUI -- not needed by pluto-tx itself
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-js8.sh [--with-js8call]
#
# Without qt6-base-dev installed system-wide (no root), point the build at unpacked headers instead:
#   QT6_INCLUDE=<dir>/usr/include/<multiarch>/qt6 [QT6_MOC=...] ./install-js8.sh   (see tools/js8ref/build.sh)
#
# Safe to re-run.
set -euo pipefail

WITH_JS8CALL=0
for arg in "$@"; do
    case "$arg" in
        --with-js8call) WITH_JS8CALL=1 ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $arg (erlaubt: --with-js8call)" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    echo "Richtig: ./install-js8.sh   (das Skript ruft sudo intern fuer apt-get auf)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JS8_DIR="$SCRIPT_DIR/js8call"
JS8_REPO="https://github.com/JS8Call-improved/JS8Call-improved.git"
JS8_TAG="v2.5.2"
JS8_COMMIT="f0f0d01b357c8eb8786aee687e8b5c787c787159"
# packaging/components.lock is the one place for the pinned version(s) (also used
# by the .deb build); the value above is the fallback if that file is missing.
LOCK="$SCRIPT_DIR/packaging/components.lock"
if [ -f "$LOCK" ]; then v="$(sed -n 's/^JS8_REPO=//p' "$LOCK")"; [ -n "$v" ] && JS8_REPO="$v"; fi
if [ -f "$LOCK" ]; then v="$(sed -n 's/^JS8_TAG=//p' "$LOCK")"; [ -n "$v" ] && JS8_TAG="$v"; fi
if [ -f "$LOCK" ]; then v="$(sed -n 's/^JS8_COMMIT=//p' "$LOCK")"; [ -n "$v" ] && JS8_COMMIT="$v"; fi

echo "== JS8 (JS8Call) installer =="
echo "Source directory: $JS8_DIR ($JS8_TAG)"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer only supports Debian/Ubuntu-family systems (apt-get not found)." >&2
    exit 1
fi

MA="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || gcc -print-multiarch 2>/dev/null || echo x86_64-linux-gnu)"
MISSING=()
command -v git >/dev/null 2>&1 || MISSING+=(git)
command -v g++ >/dev/null 2>&1 || MISSING+=(build-essential)
[ -f "${QT6_INCLUDE:-/usr/include/$MA/qt6}/QtCore/QString" ] || MISSING+=(qt6-base-dev)
[ -x "${QT6_MOC:-/usr/lib/qt6/libexec/moc}" ] || MISSING+=(qt6-base-dev-tools)
[ -f /usr/include/fftw3.h ] || MISSING+=(libfftw3-dev)
[ -f /usr/include/boost/crc.hpp ] || MISSING+=(libboost-dev)
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing missing packages: ${MISSING[*]}"
    echo "(you may be asked for your sudo password)"
    export NEEDRESTART_MODE=a
    sudo apt-get update
    sudo apt-get install -y "${MISSING[@]}"
else
    echo "Build dependencies present (git, g++, Qt6 Core headers, FFTW3, Boost)."
fi

if [ ! -d "$JS8_DIR/.git" ]; then
    echo
    echo "Cloning JS8Call-improved (~150 MB)..."
    git clone "$JS8_REPO" "$JS8_DIR"
fi
echo
echo "Checking out the pinned tag $JS8_TAG..."
git -C "$JS8_DIR" fetch --tags origin
git -C "$JS8_DIR" checkout -q "$JS8_TAG"
HEAD="$(git -C "$JS8_DIR" rev-parse HEAD)"
if [ "$HEAD" != "$JS8_COMMIT" ]; then
    echo "FEHLER: $JS8_TAG zeigt auf $HEAD, erwartet $JS8_COMMIT." >&2
    exit 1
fi

echo
echo "Extracting the JSC word lists -> js8call/jsc.json, checking pluto_tx/js8_tables.py..."
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
python3 "$SCRIPT_DIR/tools/js8_extract_tables.py" --src "$JS8_DIR" --out "$TMP" --jsc-out "$JS8_DIR/jsc.json"
if ! cmp -s "$TMP" "$SCRIPT_DIR/pluto_tx/js8_tables.py"; then
    echo "FEHLER: pluto_tx/js8_tables.py passt nicht zum Quelltext in js8call/." >&2
    exit 1
fi

echo
echo "Building the reference decoder js8ref (about 15 s)..."
"$SCRIPT_DIR/tools/js8ref/build.sh" "$JS8_DIR" "$JS8_DIR/build-js8ref"

echo
echo "Verifying: decoding tests/data/js8/wav/cq_sm0_f0.wav..."
# output captured first: with pipefail, `js8ref | grep -q` fails when grep exits early (SIGPIPE to js8ref)
REF_OUT="$("$JS8_DIR/build-js8ref/js8ref" decode "$SCRIPT_DIR/tests/data/js8/wav/cq_sm0_f0.wav" 1 || true)"
if grep -q "@ALLCALL CQ CQ CQ" <<<"$REF_OUT"; then
    echo "js8ref decodes the reference slot."
else
    echo "FEHLER: js8ref dekodiert den Referenz-Slot nicht." >&2
    exit 1
fi

echo
echo "Self-test: Python codec and imports..."
if (cd "$SCRIPT_DIR" && python3 - <<'EOF'
import sys
from pluto_tx import js8, js8_message
from pluto_advanced_rx import js8_decoder
if not js8.codec_self_test():
    sys.exit("codec does not reproduce JS8Call's reference tones")
if not js8_message.jsc_available():
    sys.exit("JSC word lists not found (js8call/jsc.json)")
backends = js8_decoder.available_backends()
if "js8" not in backends:
    sys.exit(f"js8ref not found by pluto_advanced_rx (backends: {backends})")
frames = js8_message.build_frames("DA2JH", "", "@ALLCALL JS8 SELF TEST", js8.FAST)
print(f"codec OK, JSC OK, RX decoders: {', '.join(backends)}; test message -> {len(frames)} frame(s)")
EOF
); then
    :
else
    echo "FEHLER: Selbsttest fehlgeschlagen (siehe oben)." >&2
    exit 1
fi

if [ "$WITH_JS8CALL" = "1" ]; then
    echo
    if command -v js8call >/dev/null 2>&1; then
        echo "JS8Call already installed: $(command -v js8call)"
    else
        echo "Installing the distribution's JS8Call (apt package js8call)..."
        export NEEDRESTART_MODE=a
        sudo apt-get install -y js8call
    fi
fi

echo
echo "== Done =="
echo "JS8 is available in pluto-tx, pluto-advanced-rx, pluto-cli and Web-TRX."
