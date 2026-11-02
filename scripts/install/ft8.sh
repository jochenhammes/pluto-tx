#!/usr/bin/env bash
# ft8_lib, FT8 encoding and decoding: built from source in ./ft8_lib. Run through ./install.sh. pluto_tx/ft8_ctypes.py
# finds ft8_lib/libft8wrap.so by itself.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

SRC="$REPO_DIR/ft8_lib"

apt_need make git build-essential
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" checkout -- common/monitor.c 2>/dev/null || true   # undoes the log patch below
fi
git_at_commit "$SRC" "$(lock_get FT8_LIB_REPO)" "$(lock_get FT8_LIB_COMMIT)"
# monitor_init() logs its FFT sizes to stderr at INFO level, once per decoded slot.
sed -i 's/^#define LOG_LEVEL LOG_INFO$/#define LOG_LEVEL LOG_WARN/' "$SRC/common/monitor.c"

cd "$SRC"
rm -rf .build libft8.a
make CFLAGS="-O3 -fPIC -DHAVE_STPCPY -I." libft8.a
# monitor_process() needs KissFFT, which ft8_lib's Makefile links only into its console app, and ft8_lib itself makes
# only a static archive: both go into one loadable library. No C shim needed.
mkdir -p .build/fft
for f in fft/*.c; do
    gcc -O3 -fPIC -DHAVE_STPCPY -I. -c "$f" -o ".build/${f%.c}.o"
done
gcc -shared -fPIC -Wl,--whole-archive libft8.a -Wl,--no-whole-archive .build/fft/*.o -lm -o libft8wrap.so

LD_LIBRARY_PATH="$SRC:${LD_LIBRARY_PATH:-}" python3 - <<'PY'
import ctypes
lib = ctypes.CDLL("libft8wrap.so")
for fn in ("ftx_message_encode", "ftx_message_decode", "ft8_encode", "ft4_encode", "monitor_init", "monitor_process",
           "monitor_reset", "monitor_free", "ftx_find_candidates", "ftx_decode_candidate"):
    getattr(lib, fn)
print("libft8wrap.so loads, expected functions present")
PY
