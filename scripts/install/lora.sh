#!/usr/bin/env bash
# gr-lora_sdr, the LoRa PHY of Meshtastic and MeshCore: no apt package, so built from source into ~/.local. Run through
# ./install.sh. Their packet layers (python3-meshtastic, python3-cryptography) come with the base.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

SRC="$REPO_DIR/gr-lora_sdr"
PREFIX="$HOME/.local"

# From its README: GNU Radio 3.10, cmake, volk, boost, UHD, pybind11. gnuradio-dev as in m17.sh.
apt_need cmake make git build-essential gnuradio-dev libvolk-dev libboost-all-dev libuhd-dev pybind11-dev
git_at_commit "$SRC" "$(lock_get GR_LORA_SDR_REPO)" "$(lock_get GR_LORA_SDR_COMMIT)"

mkdir -p "$SRC/build"
cd "$SRC/build"
cmake .. -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
make install

LIBDIR="$(local_lib_dir 'libgnuradio-lora_sdr.so*')"
if [ -z "$LIBDIR" ]; then
    echo "FEHLER: libgnuradio-lora_sdr.so nicht gefunden." >&2
    exit 1
fi
LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}" python3 -c \
    'from gnuradio import lora_sdr; print("gnuradio.lora_sdr imports:", lora_sdr.lora_sdr_lora_tx, lora_sdr.lora_sdr_lora_rx)'
