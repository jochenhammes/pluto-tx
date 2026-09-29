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
#
# Usage:
#   ./install.sh        # first, if you haven't already
#   ./install-js8.sh
#
# Safe to re-run.
set -euo pipefail

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
[ -x /usr/lib/qt6/libexec/moc ] || MISSING+=(qt6-base-dev-tools)
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
if "$JS8_DIR/build-js8ref/js8ref" decode "$SCRIPT_DIR/tests/data/js8/wav/cq_sm0_f0.wav" 1 | grep -q "@ALLCALL CQ CQ CQ"; then
    echo "js8ref decodes the reference slot."
else
    echo "FEHLER: js8ref dekodiert den Referenz-Slot nicht." >&2
    exit 1
fi

echo
echo "== Done =="
echo "JS8 is available in pluto-tx, pluto-advanced-rx, pluto-cli and Web-TRX."
