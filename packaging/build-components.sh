#!/usr/bin/env bash
# Builds the native third-party components for the pluto-tx package into a
# staging tree -- installs nothing into the system. Runs inside the build
# container of the target distribution (see packaging/build-deb.sh) or in
# debian/rules.
#
# Usage: packaging/build-components.sh <stage-dir> [<cache-dir>]
#
# Result below <stage-dir> (the package paths, see docs/RELEASE_PLAN.md 2):
#   usr/lib/pluto-tx/lib/     libft8wrap.so, librade.so, libgnuradio-m17.so*, libgnuradio-lora_sdr.so*
#   usr/lib/pluto-tx/bin/     lpcnet_demo, js8ref
#   usr/lib/pluto-tx/python/  gnuradio/m17, gnuradio/lora_sdr
#   usr/share/pluto-tx/js8/   jsc.json
#   usr/share/doc/pluto-tx/licenses/<component>/   the components' license files
#
# <cache-dir> keeps the git clones and downloads (Opus source, the RADE/Opus
# model) between runs; default: packaging/.cache. The versions come from
# packaging/components.lock. Build steps are the ones of the install-*.sh
# scripts; new are only the target prefix and the RPATH (/usr/lib/pluto-tx/lib),
# so no launcher has to set LD_LIBRARY_PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STAGE="$(mkdir -p "${1:?usage: build-components.sh <stage-dir> [<cache-dir>]}" && cd "$1" && pwd)"
CACHE="$(mkdir -p "${2:-$HERE/.cache}" && cd "${2:-$HERE/.cache}" && pwd)"
JOBS="${JOBS:-$(nproc)}"
# full RELRO for everything linked here (cmake, Opus's autotools and the ft8_lib gcc call)
export LDFLAGS="${LDFLAGS:-} -Wl,-z,relro -Wl,-z,now"
read -r -a LDFLAGS_ARR <<<"$LDFLAGS"

# shellcheck disable=SC1091
. "$HERE/components.lock"

PKG_LIB=/usr/lib/pluto-tx
LIBDIR="$STAGE$PKG_LIB/lib"
BINDIR="$STAGE$PKG_LIB/bin"
PYDIR="$STAGE$PKG_LIB/python"
SHARE="$STAGE/usr/share/pluto-tx"
LICENSES="$STAGE/usr/share/doc/pluto-tx/licenses"
WORK="$CACHE/work"
mkdir -p "$LIBDIR" "$BINDIR" "$PYDIR" "$SHARE/js8" "$LICENSES" "$WORK"

log() { printf '\n== %s ==\n' "$*"; }

# checkout <dir> <repo> <commit> [--recursive]: a clean tree at exactly that commit.
checkout() {
    local dir="$CACHE/src/$1" repo="$2" commit="$3"
    if [[ ! -d "$dir/.git" ]]; then
        git clone ${4:+"$4"} "$repo" "$dir"
    fi
    if ! git -C "$dir" cat-file -e "$commit^{commit}" 2>/dev/null; then
        git -C "$dir" fetch --tags origin
    fi
    git -C "$dir" checkout -q -f "$commit"
    git -C "$dir" clean -q -fdx
    if [[ "${4:-}" == "--recursive" ]]; then
        git -C "$dir" submodule update -q --init --recursive --force
        git -C "$dir" submodule foreach -q --recursive 'git clean -q -fdx'
    fi
    [[ "$(git -C "$dir" rev-parse HEAD)" == "$commit" ]] || { echo "wrong commit in $dir" >&2; exit 1; }
    echo "$dir"
}

set_rpath() {
    local f
    for f in "$@"; do
        patchelf --set-rpath "$PKG_LIB/lib" "$f"
    done
}

# An OOT module: cmake install into a scratch prefix, keep only the library
# and the Python package (no headers, cmake files, GRC blocks, docs).
build_oot() {
    local name="$1" src="$2" lib="$3" pkg="$4"
    local build="$WORK/$name-build" prefix="$WORK/$name-prefix"
    rm -rf "$build" "$prefix"
    cmake -S "$src" -B "$build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
        -DENABLE_DOXYGEN=OFF -DCMAKE_INSTALL_RPATH="$PKG_LIB/lib" -DCMAKE_SKIP_RPATH=OFF
    cmake --build "$build" -j"$JOBS"
    cmake --install "$build"
    local found
    found="$(find "$prefix" -name "$lib.so*" -printf '%h\n' | head -1)"
    [[ -n "$found" ]] || { echo "$lib.so not installed" >&2; exit 1; }
    cp -a "$found/$lib".so* "$LIBDIR/"
    local pysrc
    pysrc="$(find "$prefix" -path "*/gnuradio/$pkg/__init__.py" -printf '%h\n' | head -1)"   # not include/gnuradio/$pkg
    [[ -n "$pysrc" ]] || { echo "Python package gnuradio/$pkg not installed" >&2; exit 1; }
    mkdir -p "$PYDIR/gnuradio"
    rm -rf "${PYDIR:?}/gnuradio/$pkg"
    cp -a "$pysrc" "$PYDIR/gnuradio/$pkg"
    find "$PYDIR/gnuradio/$pkg" -name __pycache__ -prune -exec rm -rf {} +
    local so
    while IFS= read -r so; do set_rpath "$so"; done < <(find "$PYDIR/gnuradio/$pkg" -name '*.so')
    while IFS= read -r so; do set_rpath "$so"; done < <(find "$LIBDIR" -name "$lib.so*" -type f)
}

# --- gr-m17 -------------------------------------------------------------------
log "gr-m17 $GR_M17_COMMIT"
M17_SRC="$(checkout gr-m17 "$GR_M17_REPO" "$GR_M17_COMMIT" --recursive | tail -1)"
build_oot gr-m17 "$M17_SRC" libgnuradio-m17 m17
mkdir -p "$LICENSES/gr-m17"
cp "$M17_SRC/LICENSE" "$LICENSES/gr-m17/LICENSE"
for sub in libm17 codec2-mod micro-ecc tinier-aes; do
    for f in "$M17_SRC/$sub"/LICENSE*; do
        [[ -f "$f" ]] && mkdir -p "$LICENSES/gr-m17/$sub" && cp "$f" "$LICENSES/gr-m17/$sub/"
    done
done

# --- gr-lora_sdr --------------------------------------------------------------
log "gr-lora_sdr $GR_LORA_SDR_COMMIT"
LORA_SRC="$(checkout gr-lora_sdr "$GR_LORA_SDR_REPO" "$GR_LORA_SDR_COMMIT" | tail -1)"
build_oot gr-lora_sdr "$LORA_SRC" libgnuradio-lora_sdr lora_sdr
mkdir -p "$LICENSES/gr-lora_sdr"
cp "$LORA_SRC"/LICENSE* "$LICENSES/gr-lora_sdr/" 2>/dev/null || cp "$LORA_SRC"/COPYING* "$LICENSES/gr-lora_sdr/"

# --- rade_c (RADE V1: librade.so + lpcnet_demo) ---------------------------------
log "rade_c $RADE_C_COMMIT"
RADE_SRC="$(checkout rade_c "$RADE_C_REPO" "$RADE_C_COMMIT" | tail -1)"
# rade_c downloads a patched Opus snapshot, whose autogen.sh then downloads
# the ~100 MB neural model. Both are kept in the cache: the Opus zip is
# handed to cmake as a file:// URL, and a wget wrapper serves the model from
# the cache (and fills it on first use). Opus's own download script checks
# the model's SHA-256. Timeouts as in install-rade.sh -- no hanging build.
OPUS_URL_ORIG="$(sed -n 's/^set(OPUS_URL \(.*\))$/\1/p' "$RADE_SRC/cmake/BuildOpus.cmake" | head -1)"
DL="$CACHE/downloads"
mkdir -p "$DL"
OPUS_ZIP="$DL/$(basename "$OPUS_URL_ORIG")"
if [[ ! -s "$OPUS_ZIP" ]]; then
    wget --timeout=30 --tries=2 -O "$OPUS_ZIP.part" "$OPUS_URL_ORIG"
    mv "$OPUS_ZIP.part" "$OPUS_ZIP"
fi
WRAP="$WORK/wget-wrap"
mkdir -p "$WRAP"
cat >"$WRAP/wget" <<EOF
#!/usr/bin/env bash
# cached wget for the Opus model download (packaging/build-components.sh)
url="" out=""
args=("\$@")
for ((i = 0; i < \${#args[@]}; i++)); do
    case "\${args[i]}" in
        -O) out="\${args[i+1]}" ;;
        http*://*) url="\${args[i]}" ;;
    esac
done
if [[ -n "\$url" ]]; then
    name="\$(basename "\$url")"
    if [[ ! -s "$DL/\$name" ]]; then
        /usr/bin/wget --timeout=30 --tries=2 -O "$DL/\$name.part" "\$url" && mv "$DL/\$name.part" "$DL/\$name" || exit 1
    fi
    cp "$DL/\$name" "\${out:-\$name}"
    exit 0
fi
exec /usr/bin/wget --timeout=30 --tries=2 "\$@"
EOF
chmod +x "$WRAP/wget"
rm -rf "$WORK/rade-build"
PATH="$WRAP:$PATH" cmake -S "$RADE_SRC" -B "$WORK/rade-build" -DCMAKE_BUILD_TYPE=Release \
    -DOPUS_URL="file://$OPUS_ZIP"
PATH="$WRAP:$PATH" cmake --build "$WORK/rade-build" -j"$JOBS" --target rade lpcnet_demo
install -m 755 "$WORK/rade-build/src/librade.so" "$LIBDIR/librade.so"
install -m 755 "$WORK/rade-build/src/lpcnet_demo" "$BINDIR/lpcnet_demo"
set_rpath "$LIBDIR/librade.so" "$BINDIR/lpcnet_demo"
mkdir -p "$LICENSES/rade_c/opus"
cp "$RADE_SRC/LICENSE" "$LICENSES/rade_c/LICENSE"
OPUS_SRC="$(find "$WORK/rade-build" -maxdepth 4 -type f -name COPYING -path '*opus*' -printf '%h\n' | head -1)"
[[ -n "$OPUS_SRC" ]] && cp "$OPUS_SRC/COPYING" "$LICENSES/rade_c/opus/COPYING"

# --- ft8_lib (libft8wrap.so) ------------------------------------------------------
log "ft8_lib $FT8_LIB_COMMIT"
FT8_SRC="$(checkout ft8_lib "$FT8_LIB_REPO" "$FT8_LIB_COMMIT" | tail -1)"
(
    cd "$FT8_SRC"
    # as install-ft8.sh: quieter monitor log, kiss_fft objects linked in
    sed -i 's/^#define LOG_LEVEL LOG_INFO$/#define LOG_LEVEL LOG_WARN/' common/monitor.c
    make CFLAGS="-O3 -fPIC -DHAVE_STPCPY -I." libft8.a
    mkdir -p .build/fft
    for f in fft/*.c; do
        gcc -O3 -fPIC -DHAVE_STPCPY -I. -c "$f" -o ".build/${f%.c}.o"
    done
    gcc -shared -fPIC "${LDFLAGS_ARR[@]}" -Wl,--whole-archive libft8.a -Wl,--no-whole-archive .build/fft/*.o -lm -o libft8wrap.so
)
install -m 644 "$FT8_SRC/libft8wrap.so" "$LIBDIR/libft8wrap.so"
mkdir -p "$LICENSES/ft8_lib"
cp "$FT8_SRC"/LICENSE* "$LICENSES/ft8_lib/"

# --- JS8Call-improved: JSC word lists + js8ref -----------------------------------------
log "JS8Call-improved $JS8_TAG ($JS8_COMMIT)"
JS8_SRC="$(checkout js8call "$JS8_REPO" "$JS8_COMMIT" | tail -1)"
TABLES="$WORK/js8_tables.py"
python3 "$REPO/tools/js8_extract_tables.py" --src "$JS8_SRC" --out "$TABLES" --jsc-out "$SHARE/js8/jsc.json"
if ! cmp -s "$TABLES" "$REPO/pluto_tx/js8_tables.py"; then
    echo "pluto_tx/js8_tables.py does not match JS8Call $JS8_TAG -- regenerate it (see install-js8.sh)" >&2
    exit 1
fi
rm -rf "$WORK/js8ref-build"
"$REPO/tools/js8ref/build.sh" "$JS8_SRC" "$WORK/js8ref-build"
install -m 755 "$WORK/js8ref-build/js8ref" "$BINDIR/js8ref"
mkdir -p "$LICENSES/js8call"
cp "$JS8_SRC"/LICENSE* "$LICENSES/js8call/" 2>/dev/null || cp "$JS8_SRC"/COPYING* "$LICENSES/js8call/"

# --- self-check ------------------------------------------------------------------
log "Self-check"
REF_OUT="$(LC_ALL=C.UTF-8 "$BINDIR/js8ref" decode "$REPO/tests/data/js8/wav/cq_sm0_f0.wav" 1 2>/dev/null || true)"
grep -q "@ALLCALL CQ CQ CQ" <<<"$REF_OUT" || { echo "js8ref does not decode the reference WAV" >&2; exit 1; }
python3 - "$LIBDIR" <<'EOF'
import ctypes, sys
lib = sys.argv[1]
ft8 = ctypes.CDLL(f"{lib}/libft8wrap.so")
for fn in ("ftx_message_encode", "ftx_message_decode", "ft8_encode", "monitor_init", "ftx_decode_candidate"):
    getattr(ft8, fn)
rade = ctypes.CDLL(f"{lib}/librade.so")
for fn in ("rade_open", "rade_tx", "rade_rx", "rade_close"):
    getattr(rade, fn)
print("libft8wrap.so and librade.so load")
EOF
# the blocks themselves, not just the package: the OOT __init__.py swallows
# an ImportError of its pybind11 module
# (LD_LIBRARY_PATH only here: the RPATH points to the final /usr/lib/pluto-tx/lib)
(cd / && LD_LIBRARY_PATH="$LIBDIR" PYTHONPATH="$PYDIR" python3 -P -c "
from gnuradio import m17, lora_sdr
print('gnuradio.m17:', m17.m17_coder, m17.m17_decoder, m17.codec2_encoder, m17.codec2_decoder)
print('gnuradio.lora_sdr:', lora_sdr.lora_sdr_lora_tx, lora_sdr.lora_sdr_lora_rx)")
du -sh "$STAGE$PKG_LIB" "$SHARE/js8"
echo "Done: $STAGE"
