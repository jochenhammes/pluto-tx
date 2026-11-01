#!/usr/bin/env bash
# Optional, separate installer for JTTY support in pluto-tx, pluto-advanced-rx, pluto-cli and Web-TRX
# (docs/jtty/SPEC.md). Same reasoning as install-js8.sh: install.sh stays pure apt.
#
# What it does:
#   1. downloads the WSJT-X project's codec tarball at the pinned tag, checks its SHA256
#   2. unpacks it into ./wsjt-codec (gitignored): bin/wsjt-codec with its bundled libraries in lib/
#   3. self-test: renders a message, decodes it back, and decodes the WSJT-X sample recording; that also
#      saves the FFT plans, so the first reception doesn't stall while they are measured
#
# An installed wsjtx package and its jt9 (the FT8 decoder of this project) are not touched.
#
# Usage:
#   ./install-jtty.sh
#
# Safe to re-run.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEC_DIR="$SCRIPT_DIR/wsjt-codec"
WSJT_CODEC_TAG="v3.3.0-beta5"
WSJT_CODEC_COMMIT="d89fe30ca1dfd3c38def8f17228f09f042829f95"
WSJT_CODEC_SHA256_X86_64="081cc357c5b69e5a6c55ffefc500cd989a0d2cbf2ab64da6e15bd31e4daddedf"
WSJT_CODEC_SHA256_AARCH64="7da722fa9c663a6f83f50e9b298ecf204bbb5a82988c889416e97f186c70580b"
# packaging/components.lock is the one place for the pinned version(s) (also used
# by the .deb build); the values above are the fallback if that file is missing.
LOCK="$SCRIPT_DIR/packaging/components.lock"
if [ -f "$LOCK" ]; then v="$(sed -n 's/^WSJT_CODEC_TAG=//p' "$LOCK")"; [ -n "$v" ] && WSJT_CODEC_TAG="$v"; fi
if [ -f "$LOCK" ]; then v="$(sed -n 's/^WSJT_CODEC_COMMIT=//p' "$LOCK")"; [ -n "$v" ] && WSJT_CODEC_COMMIT="$v"; fi
if [ -f "$LOCK" ]; then v="$(sed -n 's/^WSJT_CODEC_SHA256_X86_64=//p' "$LOCK")"; [ -n "$v" ] && WSJT_CODEC_SHA256_X86_64="$v"; fi
if [ -f "$LOCK" ]; then v="$(sed -n 's/^WSJT_CODEC_SHA256_AARCH64=//p' "$LOCK")"; [ -n "$v" ] && WSJT_CODEC_SHA256_AARCH64="$v"; fi

case "$(uname -m)" in
    x86_64) ARCH=x86_64; SHA="$WSJT_CODEC_SHA256_X86_64" ;;
    aarch64) ARCH=aarch64; SHA="$WSJT_CODEC_SHA256_AARCH64" ;;
    *) echo "FEHLER: Fuer $(uname -m) gibt es keinen wsjt-codec; JTTY bleibt deaktiviert." >&2; exit 1 ;;
esac

echo "== JTTY installer =="
echo "wsjt-codec $WSJT_CODEC_TAG ($WSJT_CODEC_COMMIT), $ARCH -> $CODEC_DIR"
NAME="wsjtx-${WSJT_CODEC_TAG#v}-linux-$ARCH-codec.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fL --retry 3 -o "$TMP/$NAME" "https://github.com/WSJTX/wsjtx/releases/download/$WSJT_CODEC_TAG/$NAME"
echo "$SHA  $TMP/$NAME" | sha256sum -c -
mkdir -p "$TMP/unpacked"
tar xzf "$TMP/$NAME" -C "$TMP/unpacked" --strip-components=1
rm -rf "$CODEC_DIR"
mv "$TMP/unpacked" "$CODEC_DIR"

echo
echo "Self-test: render, decode, sample recording..."
(cd "$SCRIPT_DIR" && python3 - <<'EOF'
import sys
from pluto_tx import jtty_codec
if not jtty_codec.self_test():
    sys.exit("wsjt-codec does not decode what it rendered")
text = jtty_codec.decode_pcm_file("tests/data/jtty/260807_134110.wav")
if not any("NO FALSE DECODES!" in t for t in text):
    sys.exit(f"wsjt-codec does not decode the sample recording: {text}")
print(f"wsjt-codec works: {jtty_codec.codec_program()}")
EOF
)
echo "JTTY is ready."
