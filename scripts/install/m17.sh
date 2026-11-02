#!/usr/bin/env bash
# gr-m17, M17 digital voice: no apt package, so built from source into ~/.local. Run through ./install.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

SRC="$REPO_DIR/gr-m17"
PREFIX="$HOME/.local"

# gnuradio-dev is only a Recommends of gnuradio, and without it cmake fails with "Gnuradio config not found".
apt_need cmake make doxygen git build-essential gnuradio-dev
# Pinned: gr-m17's README only claims GNU Radio 3.10.9 and 3.10.10; it is verified here at this commit.
git_at_commit "$SRC" "$(lock_get GR_M17_REPO)" "$(lock_get GR_M17_COMMIT)" --recursive

mkdir -p "$SRC/build"
cd "$SRC/build"
cmake .. -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
make install

LIBDIR="$(local_lib_dir 'libgnuradio-m17.so*')"
if [ -z "$LIBDIR" ]; then
    echo "FEHLER: libgnuradio-m17.so nicht gefunden." >&2
    exit 1
fi
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 -c \
    'from gnuradio import m17; print("gnuradio.m17 imports:", m17.m17_coder, m17.m17_decoder, m17.codec2_encoder, m17.codec2_decoder)'
