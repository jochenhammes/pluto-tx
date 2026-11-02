#!/usr/bin/env bash
# JTTY: the WSJT-X project's own codec binaries, with their bundled libraries, in ./wsjt-codec. Run through ./install.sh.
# An installed wsjtx package and its jt9 are not touched. The self-test also saves the FFT plans, so the first reception
# does not stall while they are measured.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

CODEC_DIR="$REPO_DIR/wsjt-codec"
TAG="$(lock_get WSJT_CODEC_TAG)"
case "$(uname -m)" in
    x86_64) ARCH=x86_64; SHA="$(lock_get WSJT_CODEC_SHA256_X86_64)" ;;
    aarch64) ARCH=aarch64; SHA="$(lock_get WSJT_CODEC_SHA256_AARCH64)" ;;
    *) echo "Fuer $(uname -m) gibt es keinen wsjt-codec, JTTY bleibt aus."; exit "$EXIT_SKIPPED" ;;
esac

apt_need curl
NAME="wsjtx-${TAG#v}-linux-$ARCH-codec.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fL --retry 3 -o "$TMP/$NAME" "https://github.com/WSJTX/wsjtx/releases/download/$TAG/$NAME"
echo "$SHA  $TMP/$NAME" | sha256sum -c -
mkdir -p "$TMP/unpacked"
tar xzf "$TMP/$NAME" -C "$TMP/unpacked" --strip-components=1
rm -rf "$CODEC_DIR"
mv "$TMP/unpacked" "$CODEC_DIR"

cd "$REPO_DIR"
python3 - <<'PY'
import sys
from pluto_tx import jtty_codec
if not jtty_codec.self_test():
    sys.exit("wsjt-codec does not decode what it rendered")
text = jtty_codec.decode_pcm_file("tests/data/jtty/260807_134110.wav")
if not any("NO FALSE DECODES!" in t for t in text):
    sys.exit(f"wsjt-codec does not decode the sample recording: {text}")
print(f"wsjt-codec works: {jtty_codec.codec_program()}")
PY
