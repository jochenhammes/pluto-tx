#!/usr/bin/env bash
# JS8: clones JS8Call-improved into ./js8call for its word lists and builds its decoder as js8ref. Run through ./install.sh.
# Without qt6-base-dev installed system-wide, point the build at unpacked headers: QT6_INCLUDE=<dir>/usr/include/<multiarch>/qt6
# [QT6_MOC=...]; see tools/js8ref/build.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

SRC="$REPO_DIR/js8call"
TAG="$(lock_get JS8_TAG)"
COMMIT="$(lock_get JS8_COMMIT)"
MA="$(multiarch)"

packages=(git build-essential libfftw3-dev libboost-dev)
[ -f "${QT6_INCLUDE:-/usr/include/$MA/qt6}/QtCore/QString" ] || packages+=(qt6-base-dev)
[ -x "${QT6_MOC:-/usr/lib/qt6/libexec/moc}" ] || packages+=(qt6-base-dev-tools)
apt_need "${packages[@]}"

git_at_commit "$SRC" "$(lock_get JS8_REPO)" "$COMMIT"
git -C "$SRC" fetch -q --tags origin 2>/dev/null || true
if [ "$(git -C "$SRC" rev-parse "$TAG^{commit}" 2>/dev/null)" != "$COMMIT" ]; then
    echo "FEHLER: $TAG zeigt nicht auf $COMMIT." >&2
    exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
python3 "$REPO_DIR/tools/js8_extract_tables.py" --src "$SRC" --out "$TMP" --jsc-out "$SRC/jsc.json"
if ! cmp -s "$TMP" "$REPO_DIR/pluto_tx/js8_tables.py"; then
    echo "FEHLER: pluto_tx/js8_tables.py passt nicht zum Quelltext in js8call/." >&2
    exit 1
fi

"$REPO_DIR/tools/js8ref/build.sh" "$SRC" "$SRC/build-js8ref"

# output captured first: with pipefail, `js8ref | grep -q` fails when grep exits early (SIGPIPE to js8ref)
REF_OUT="$("$SRC/build-js8ref/js8ref" decode "$REPO_DIR/tests/data/js8/wav/cq_sm0_f0.wav" 1 || true)"
if ! grep -q "@ALLCALL CQ CQ CQ" <<<"$REF_OUT"; then
    echo "FEHLER: js8ref dekodiert den Referenz-Slot nicht." >&2
    exit 1
fi

cd "$REPO_DIR"
python3 - <<'PY'
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
PY
