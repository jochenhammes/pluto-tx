#!/usr/bin/env bash
# rade_c, RADE V1 digital voice: built from source in ./rade_c, with its own patched Opus/FARGAN fork. Run through
# ./install.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

SRC="$REPO_DIR/rade_c"

# autoconf, automake, libtool: the Opus fork's autogen.sh. wget: that script's download of the FARGAN/LPCNet model.
apt_need cmake make git build-essential autoconf automake libtool wget
git_at_commit "$SRC" "$(lock_get RADE_C_REPO)" "$(lock_get RADE_C_COMMIT)"

# download_model.sh fetches about 100 MB with a plain wget and no timeout, from a host of its own: when that was down,
# the build hung without an error. It sits in a source tree cmake only unpacks later, so the fix goes on the PATH.
WGET_WRAP_DIR="$(mktemp -d)"
trap 'rm -rf "$WGET_WRAP_DIR"' EXIT
cat >"$WGET_WRAP_DIR/wget" <<'WGET_EOF'
#!/bin/sh
exec /usr/bin/wget --timeout=30 --tries=2 "$@"
WGET_EOF
chmod +x "$WGET_WRAP_DIR/wget"
export PATH="$WGET_WRAP_DIR:$PATH"

mkdir -p "$SRC/build"
cd "$SRC/build"
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j"$(nproc)"

LIBDIR="$SRC/build/src"
if [ ! -f "$LIBDIR/librade.so" ] || [ ! -x "$LIBDIR/lpcnet_demo" ]; then
    echo "FEHLER: librade.so oder lpcnet_demo fehlt in $LIBDIR." >&2
    exit 1
fi
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 - <<'PY'
import ctypes
lib = ctypes.CDLL("librade.so")
for fn in ("rade_open", "rade_tx", "rade_rx", "rade_close"):
    getattr(lib, fn)
print("librade.so loads, rade_api.h surface present")
PY
